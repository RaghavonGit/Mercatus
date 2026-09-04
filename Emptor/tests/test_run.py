import sys

import httpx
import pytest

from fides import FidesError

import emptor.run as run_module
from emptor.config import Config, ConfigError, LLMSettings
from emptor.decide import DecideResult
from emptor.discover import DiscoverError
from emptor.purchase import PendingPurchase, PurchaseError
from emptor.run import _safe_log_event

# The autouse _no_real_llm_preflight fixture (conftest.py) replaces
# run_module.preflight_llm with a no-op. Capture the real function here, at
# import, for the tests that exercise preflight_llm itself.
_real_preflight_llm = run_module.preflight_llm

CATALOG = [{"id": "a", "name": "Widget", "price_inr": 100, "in_stock": True}]
LLM = LLMSettings(
    base_url="http://llm.test/v1", model="m", fallback_model="m-small", api_key=None
)
CONFIG = Config(llm=LLM, mercator_endpoint="http://shop.test", default_budget_inr=1000)

PENDING = PendingPurchase(
    payment_link_id="plink-1",
    payment_link_url="https://rzp.io/i/plink-1",
    total_inr=100,
    expire_hours=6,
)

RUPEE = "₹"


class _FakeConnection:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def __aenter__(self):
        return "fake-session"

    async def __aexit__(self, *exc_info):
        return None


def _wire_happy_path(monkeypatch, *, purchase_result=PENDING):
    """discover -> decide -> purchase, stubbed to the happy path. Callers
    still set assume_yes=True or stub builtins.input for the approval gate."""
    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, llm):
        return DecideResult(picks=[{"product_id": "a", "quantity": 1}], reasoning="test reason")

    async def fake_purchase(session, validated):
        if isinstance(purchase_result, Exception):
            raise purchase_result
        return purchase_result

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)


# --- pending / approval outcomes ----------------------------------------


async def test_run_pending_path(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "PENDING: pay INR 100 at https://rzp.io/i/plink-1" in out
    assert "link id plink-1" in out
    assert "expires in ~6h" in out


async def test_run_operator_types_yes_proceeds(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 0
    assert "PENDING" in capsys.readouterr().out


@pytest.mark.parametrize("typed", ["", "y", "YES", " yes ", "Y", "yep", "no"])
async def test_run_only_exact_yes_confirms(monkeypatch, capsys, typed):
    _wire_happy_path(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt="": typed)

    called = {"n": 0}
    inner = run_module.purchase

    async def counting(session, validated):
        called["n"] += 1
        return await inner(session, validated)

    monkeypatch.setattr(run_module, "purchase", counting)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 0  # a decline is not a failure
    assert "DECLINED: purchase not confirmed by operator" in capsys.readouterr().err
    assert called["n"] == 0  # purchase() never reached


async def test_run_decline_logs_purchase_declined_event(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    events = []
    monkeypatch.setattr(
        run_module, "_safe_log_event", lambda data, *, event_type: events.append((event_type, data))
    )

    await run_module.run("buy a widget", 1000, CONFIG)

    assert (
        "purchase_declined",
        {
            "goal": "buy a widget",
            "picks": [{"product_id": "a", "quantity": 1}],
            "total_inr": 100,
        },
    ) in events


async def test_run_yes_flag_skips_the_prompt_entirely(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)

    def boom(_prompt=""):
        raise AssertionError("input() must not be called with --yes")

    monkeypatch.setattr("builtins.input", boom)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)
    assert exit_code == 0
    assert "PENDING" in capsys.readouterr().out


# --- preflight LLM check ---------------------------------------------------


async def test_run_blocks_when_llm_endpoint_is_unreachable(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)

    async def down(*a, **k):
        raise run_module.PreflightError(
            "LLM endpoint http://127.0.0.1:11434/v1 is not reachable (ConnectError). "
            "Start it, e.g. 'ollama serve'."
        )

    class _ShopMustNotBeDialed:
        def __init__(self, endpoint):
            raise AssertionError("preflight must run before ShopConnection")

    monkeypatch.setattr(run_module, "preflight_llm", down)
    monkeypatch.setattr(run_module, "ShopConnection", _ShopMustNotBeDialed)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    # the preflight BLOCK wins -- the shop is never dialed
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err and "ollama serve" in err


async def test_preflight_llm_warns_but_proceeds_when_model_not_listed(capsys):
    def handler(request):
        return httpx.Response(200, json={"object": "list", "data": [{"id": "some-other-model"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _real_preflight_llm(CONFIG.llm, client=client)  # must not raise
    await client.aclose()

    assert "WARNING" in capsys.readouterr().err


async def test_preflight_llm_raises_on_non_200():
    def handler(request):
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(run_module.PreflightError):
        await _real_preflight_llm(CONFIG.llm, client=client)
    await client.aclose()


async def test_preflight_llm_passes_when_model_is_listed(capsys):
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "m"}, {"id": "m-small"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await _real_preflight_llm(CONFIG.llm, client=client)
    await client.aclose()

    assert "WARNING" not in capsys.readouterr().err


# --- --picks manual override ---------------------------------------------


async def test_run_picks_override_skips_the_llm_and_reaches_purchase(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)

    async def decide_must_not_run(*a, **k):
        raise AssertionError("decide() must not be called with --picks")

    async def preflight_must_not_run(*a, **k):
        raise AssertionError("preflight must not run with --picks")

    monkeypatch.setattr(run_module, "decide", decide_must_not_run)
    monkeypatch.setattr(run_module, "preflight_llm", preflight_must_not_run)

    exit_code = await run_module.run(
        "buy a widget",
        1000,
        CONFIG,
        assume_yes=True,
        picks_override='[{"product_id": "a", "quantity": 1}]',
    )

    assert exit_code == 0
    assert "PENDING" in capsys.readouterr().out


async def test_run_picks_override_invalid_json_is_blocked(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)
    exit_code = await run_module.run(
        "buy a widget", 1000, CONFIG, assume_yes=True, picks_override="not json"
    )
    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_picks_override_non_array_is_blocked(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)
    exit_code = await run_module.run(
        "buy a widget", 1000, CONFIG, assume_yes=True, picks_override='{"product_id": "a"}'
    )
    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_picks_override_out_of_catalog_id_still_rejected(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)
    exit_code = await run_module.run(
        "buy a widget",
        1000,
        CONFIG,
        assume_yes=True,
        picks_override='[{"product_id": "not-in-catalog", "quantity": 1}]',
    )
    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_picks_override_still_prompts_for_confirmation(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _p="": "no")

    exit_code = await run_module.run(
        "buy a widget", 1000, CONFIG, picks_override='[{"product_id": "a", "quantity": 1}]'
    )

    assert exit_code == 0
    assert "DECLINED" in capsys.readouterr().err


async def test_run_picks_override_logs_manual_source(monkeypatch):
    _wire_happy_path(monkeypatch)
    events = []
    monkeypatch.setattr(
        run_module, "_safe_log_event", lambda data, *, event_type: events.append((event_type, data))
    )

    await run_module.run(
        "buy a widget",
        1000,
        CONFIG,
        assume_yes=True,
        picks_override='[{"product_id": "a", "quantity": 1}]',
    )

    llm_decision = next(d for t, d in events if t == "llm_decision")
    assert llm_decision["source"] == "manual-override"
    assert "no LLM call" in llm_decision["reasoning"]


async def test_run_llm_path_logs_the_models_reasoning(monkeypatch):
    _wire_happy_path(monkeypatch)  # fake_decide returns reasoning="test reason"
    events = []
    monkeypatch.setattr(
        run_module, "_safe_log_event", lambda data, *, event_type: events.append((event_type, data))
    )

    await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    llm_decision = next(d for t, d in events if t == "llm_decision")
    assert llm_decision["source"] == "llm"
    assert llm_decision["reasoning"] == "test reason"


# --- blocked outcomes --------------------------------------------------


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

    async def fake_decide(goal, budget_inr, catalog, llm):
        return DecideResult(picks=[{"product_id": "does-not-exist", "quantity": 1}], reasoning="x")

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG)

    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_blocks_on_purchase_error(monkeypatch, capsys):
    _wire_happy_path(monkeypatch, purchase_result=PurchaseError("checkout exploded"))

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    assert exit_code == 1
    assert "BLOCKED" in capsys.readouterr().err


async def test_run_unwraps_exceptiongroup_into_readable_blocked_message(monkeypatch, capsys):
    """A dead shop connection surfaces as an ExceptionGroup (anyio TaskGroup
    internals) whose default str() tells a human nothing. The BLOCKED line
    must surface the real cause instead."""

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


# --- "already went through" must never become BLOCKED -----------------


async def test_run_reports_pending_even_if_pending_reporting_raises(monkeypatch, capsys):
    """The checkout already went through -- a formatting crash in the
    PENDING line must never become BLOCKED/exit 1 (baiting a retry that
    mints a fresh idempotency key)."""
    _wire_happy_path(monkeypatch)

    def exploding_report(pending):
        raise UnicodeEncodeError("charmap", RUPEE, 0, 1, "boom")

    monkeypatch.setattr(run_module, "_report_pending", exploding_report)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "BLOCKED" not in captured.err
    assert "plink-1" in captured.out


async def test_run_reports_pending_even_if_checkout_log_call_raises(monkeypatch, capsys):
    """Mirrors the above but targets the checkout_result ledger call site:
    it sits after purchased_pending_link is assigned, so the outer handler
    still reports PENDING even if the log call blows up past _safe_log_event's
    own swallow."""
    _wire_happy_path(monkeypatch)

    def exploding_safe_log(data, *, event_type):
        if event_type == "checkout_result":
            raise RuntimeError("totally unexpected bug")

    monkeypatch.setattr(run_module, "_safe_log_event", exploding_safe_log)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "BLOCKED" not in captured.err
    assert "plink-1" in captured.out


async def test_run_pending_message_is_ascii_safe_with_hostile_link(monkeypatch, capsys):
    hostile = PendingPurchase(
        payment_link_id="plink-" + RUPEE,
        payment_link_url="https://rzp.io/i/" + RUPEE,
        total_inr=100,
        expire_hours=6,
    )
    _wire_happy_path(monkeypatch, purchase_result=hostile)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "PENDING" in out
    assert out == out.encode("ascii", errors="strict").decode("ascii")
    assert "INR 100" in out


# --- misc invariants --------------------------------------------------


async def test_run_makes_exactly_one_llm_call(monkeypatch, capsys):
    call_count = {"n": 0}

    monkeypatch.setattr(run_module, "ShopConnection", _FakeConnection)

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget_inr, catalog, llm):
        call_count["n"] += 1
        return DecideResult(picks=[{"product_id": "a", "quantity": 1}], reasoning="one call")

    async def fake_purchase(session, validated):
        return PENDING

    monkeypatch.setattr(run_module, "discover_catalog", fake_discover)
    monkeypatch.setattr(run_module, "decide", fake_decide)
    monkeypatch.setattr(run_module, "purchase", fake_purchase)

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    assert exit_code == 0
    assert call_count["n"] == 1


def test_main_uses_default_budget_when_budget_flag_omitted(monkeypatch, capsys):
    seen = {}

    async def fake_run(goal, budget_inr, config, assume_yes=False, picks_override=None):
        seen["goal"] = goal
        seen["budget_inr"] = budget_inr
        seen["assume_yes"] = assume_yes
        seen["picks_override"] = picks_override
        return 0

    monkeypatch.setattr(run_module, "load_config", lambda: CONFIG)
    monkeypatch.setattr(run_module, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["emptor", "buy a widget"])

    with pytest.raises(SystemExit) as exc_info:
        run_module.main()

    assert exc_info.value.code == 0
    assert seen["budget_inr"] == CONFIG.default_budget_inr
    assert seen["assume_yes"] is False
    assert seen["picks_override"] is None


def test_main_passes_yes_flag_through(monkeypatch):
    seen = {}

    async def fake_run(goal, budget_inr, config, assume_yes=False, picks_override=None):
        seen["assume_yes"] = assume_yes
        return 0

    monkeypatch.setattr(run_module, "load_config", lambda: CONFIG)
    monkeypatch.setattr(run_module, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["emptor", "buy a widget", "--yes"])

    with pytest.raises(SystemExit):
        run_module.main()

    assert seen["assume_yes"] is True


def test_main_passes_picks_flag_through(monkeypatch):
    seen = {}

    async def fake_run(goal, budget_inr, config, assume_yes=False, picks_override=None):
        seen["picks_override"] = picks_override
        return 0

    monkeypatch.setattr(run_module, "load_config", lambda: CONFIG)
    monkeypatch.setattr(run_module, "run", fake_run)
    monkeypatch.setattr(
        sys, "argv", ["emptor", "buy a widget", "--picks", '[{"product_id": "a", "quantity": 1}]']
    )

    with pytest.raises(SystemExit):
        run_module.main()

    assert seen["picks_override"] == '[{"product_id": "a", "quantity": 1}]'


def test_main_blocks_on_config_error(monkeypatch, capsys):
    def fake_load_config():
        raise ConfigError("DEFAULT_BUDGET_INR must be an integer, got 'nope'")

    monkeypatch.setattr(run_module, "load_config", fake_load_config)
    monkeypatch.setattr(sys, "argv", ["emptor", "buy a widget"])

    with pytest.raises(SystemExit) as exc_info:
        run_module.main()

    assert exc_info.value.code == 1
    assert "BLOCKED" in capsys.readouterr().err


# --- Fides ledger integration ----------------------------------------------


async def test_run_logs_all_five_events_in_order_on_pending_path(monkeypatch, capsys):
    _wire_happy_path(monkeypatch)
    calls = []
    monkeypatch.setattr(
        run_module,
        "_safe_log_event",
        lambda data, *, event_type: calls.append((event_type, data)),
    )

    exit_code = await run_module.run("buy a widget", 1000, CONFIG, assume_yes=True)

    assert exit_code == 0
    event_types = [event_type for event_type, _ in calls]
    assert event_types == [
        "goal_received", "catalog_retrieved", "llm_decision",
        "validation_result", "checkout_result",
    ]
    data_by_type = dict(calls)
    assert data_by_type["goal_received"]["goal"] == "buy a widget"
    assert data_by_type["catalog_retrieved"] == {"catalog_size": 1, "affordable_count": 1}
    assert data_by_type["validation_result"]["ok"] is True
    assert data_by_type["checkout_result"]["payment_link_id"] == "plink-1"
    assert data_by_type["checkout_result"]["status"] == "pending"


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
