import sys

import pytest

from fides import FidesError

import emptor.run as run_module
from emptor.config import Config, ConfigError
from emptor.decide import DecideError
from emptor.discover import DiscoverError
from emptor.purchase import PurchaseError
from emptor.run import _safe_log_event

CATALOG = [{"id": "a", "name": "Widget", "price_inr": 100, "in_stock": True}]
CONFIG = Config(nim_api_key="k", mercator_endpoint="http://shop.test", default_budget_inr=1000)


class _FakeConnection:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def __aenter__(self):
        return "fake-session"

    async def __aexit__(self, *exc_info):
        return None


async def test_run_success_path(monkeypatch, capsys):
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, api_key):
        return [{"product_id": "a", "quantity": 1}]

    async def fake_purchase(session, validated):
        return "order-1"

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "SUCCESS" in out
    assert "order-1" in out


async def test_run_blocks_when_catalog_empty_after_filter(monkeypatch, capsys):
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        return [{"id": "a", "name": "Expensive", "price_inr": 999999, "in_stock": True}]

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_blocks_on_discover_error(monkeypatch, capsys):
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        raise DiscoverError("shop is broken")

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_blocks_when_validation_fails(monkeypatch, capsys):
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, api_key):
        return [{"product_id": "does-not-exist", "quantity": 1}]

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_blocks_on_purchase_error(monkeypatch, capsys):
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, api_key):
        return [{"product_id": "a", "quantity": 1}]

    async def fake_purchase(session, validated):
        raise PurchaseError("checkout exploded")

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_reports_success_even_if_success_reporting_raises(monkeypatch, capsys):
    """A completed purchase must never be reported as BLOCKED.

    Regression test: printing the success line used to be able to raise
    (e.g. UnicodeEncodeError on a cp1252 console, inducible by a hostile
    product name), which the blanket handler turned into BLOCKED/exit 1 --
    baiting a retry that would mint a fresh idempotency key and double-charge.
    """
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, api_key):
        return [{"product_id": "a", "quantity": 1}]

    async def fake_purchase(session, validated):
        return "order-1"

    def exploding_report(validated, order_id):
        raise UnicodeEncodeError("charmap", "₹", 0, 1, "boom")

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)
    monkeypatch.setattr(run_module, "_report_success", exploding_report)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "BLOCKED" not in captured.err
    assert "order-1" in captured.out


async def test_run_success_message_is_ascii_safe_with_hostile_product_name(monkeypatch, capsys):
    """Untrusted shop-supplied names are sanitized, and no raw rupee sign is printed."""
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        return [{"id": "a", "name": "Widget ₹", "price_inr": 100, "in_stock": True}]

    async def fake_decide(goal, budget_inr, catalog, api_key):
        return [{"product_id": "a", "quantity": 1}]

    async def fake_purchase(session, validated):
        return "order-1"

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "SUCCESS" in out
    # No non-ASCII survives into user-facing output.
    assert out == out.encode("ascii", errors="strict").decode("ascii")
    assert "INR 100" in out


async def test_run_unwraps_exceptiongroup_into_readable_blocked_message(monkeypatch, capsys):
    """A dead shop connection surfaces as an ExceptionGroup (anyio TaskGroup
    internals) whose default str() - "unhandled errors in a TaskGroup
    (1 sub-exception)" - tells a human nothing. Confirmed live: running the
    real CLI with no shop server listening produced exactly that message.
    The BLOCKED line must surface the real cause instead.
    """

    class _FailingConnection:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        async def __aenter__(self):
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup", [ConnectionRefusedError("no server")]
            )

        async def __aexit__(self, *exc_info):
            return None

    monkeypatch.setattr(run_module, "ShopConnection", _FailingConnection)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "BLOCKED" in err
    assert "ConnectionRefusedError" in err
    assert "no server" in err
    assert "TaskGroup" not in err


async def test_run_makes_exactly_one_llm_call(monkeypatch, capsys):
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    call_count = {"n": 0}

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, api_key):
        call_count["n"] += 1
        return [{"product_id": "a", "quantity": 1}]

    async def fake_purchase(session, validated):
        return "order-1"

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 0
    assert call_count["n"] == 1


def test_main_uses_default_budget_when_budget_flag_omitted(monkeypatch, capsys):
    seen = {}

    async def fake_run(goal, budget_inr, config):
        seen["goal"] = goal
        seen["budget_inr"] = budget_inr
        return 0

    monkeypatch.setattr(run_module, "load_config", lambda: CONFIG)
    monkeypatch.setattr(run_module, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["emptor", "buy a widget"])

    with pytest.raises(SystemExit) as exc_info:
        run_module.main()

    assert exc_info.value.code == 0
    assert seen["budget_inr"] == CONFIG.default_budget_inr


def test_main_blocks_on_config_error(monkeypatch, capsys):
    def fake_load_config():
        raise ConfigError("NIM_API_KEY is not set (check your .env file)")

    monkeypatch.setattr(run_module, "load_config", fake_load_config)
    monkeypatch.setattr(sys, "argv", ["emptor", "buy a widget"])

    with pytest.raises(SystemExit) as exc_info:
        run_module.main()

    assert exc_info.value.code == 1
    assert "BLOCKED" in capsys.readouterr().err


# --- Fides ledger integration ----------------------------------------------


async def test_run_logs_all_five_events_in_order_on_success_path(monkeypatch, capsys):
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)
    calls = []
    monkeypatch.setattr(
        run_module,
        "_safe_log_event",
        lambda data, *, event_type: calls.append((event_type, data)),
    )

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, api_key):
        return [{"product_id": "a", "quantity": 1}]

    async def fake_purchase(session, validated):
        return "order-1"

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 0
    event_types = [event_type for event_type, _ in calls]
    assert event_types == [
        "goal_received", "catalog_retrieved", "llm_decision",
        "validation_result", "checkout_result",
    ]
    data_by_type = dict(calls)
    assert data_by_type["goal_received"]["goal"] == "buy a widget"
    assert data_by_type["goal_received"]["budget_inr"] == 1000
    assert data_by_type["catalog_retrieved"] == {"catalog_size": 1, "affordable_count": 1}
    assert data_by_type["llm_decision"]["picks"] == [{"product_id": "a", "quantity": 1}]
    assert data_by_type["validation_result"]["ok"] is True
    assert data_by_type["checkout_result"]["order_id"] == "order-1"


async def test_run_reports_success_even_if_checkout_log_call_raises(monkeypatch, capsys):
    """A completed purchase must never be reported as BLOCKED, even if the
    checkout_result ledger write itself blows up in a way that escapes
    _safe_log_event's own internal swallow -- the call site sits after
    purchased_order_id is assigned, so the outer handler still reports
    SUCCESS. Mirrors test_run_reports_success_even_if_success_reporting_raises
    but targets the ledger call site.
    """
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    def exploding_safe_log(data, *, event_type):
        if event_type == "checkout_result":
            raise RuntimeError("totally unexpected bug")

    monkeypatch.setattr(run_module, "_safe_log_event", exploding_safe_log)

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, api_key):
        return [{"product_id": "a", "quantity": 1}]

    async def fake_purchase(session, validated):
        return "order-1"

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "BLOCKED" not in captured.err
    assert "order-1" in captured.out


def test_safe_log_event_swallows_fides_errors(monkeypatch, capsys):
    def raising_log_event(data, *, actor, event_type):
        raise FidesError("db is locked")

    monkeypatch.setattr(run_module, "log_event", raising_log_event)

    _safe_log_event({"x": 1}, event_type="goal_received")  # must not raise

    assert "WARNING" in capsys.readouterr().err


def test_safe_log_event_passes_actor_emptor(monkeypatch):
    seen = {}

    def fake_log_event(data, *, actor, event_type):
        seen["actor"] = actor
        seen["event_type"] = event_type

    monkeypatch.setattr(run_module, "log_event", fake_log_event)
    _safe_log_event({}, event_type="goal_received")
    assert seen["actor"] == "emptor"
    assert seen["event_type"] == "goal_received"
