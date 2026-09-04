"""API-surface smoke tests via FastAPI's TestClient. Razorpay and the shop
are stubbed."""

import dataclasses

import forum.app as app_module
import forum.story as story
from fastapi.testclient import TestClient
from fides.ledger import Ledger

client = TestClient(app_module.app)


def _with_config(monkeypatch, **overrides):
    monkeypatch.setattr(app_module, "CONFIG", dataclasses.replace(app_module.CONFIG, **overrides))


def test_index_serves_the_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "Mercatus" in r.text


def test_payment_endpoint_passes_through_fetch_payment_link(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "fetch_payment_link",
        lambda cli, link_id: {"payment_link_id": link_id, "status": "paid", "amount": 450, "amount_paid": 450},
    )
    r = client.get("/api/payment/plink_abc")
    assert r.status_code == 200
    assert r.json() == {
        "payment_link_id": "plink_abc",
        "status": "paid",
        "amount": 450,
        "amount_paid": 450,
    }


def test_payment_endpoint_502s_on_razorpay_error(monkeypatch):
    def boom(cli, link_id):
        raise RuntimeError("network")

    monkeypatch.setattr(app_module, "fetch_payment_link", boom)
    r = client.get("/api/payment/x")
    assert r.status_code == 502


def test_spend_endpoint_without_a_db_returns_zero(monkeypatch, tmp_path):
    _with_config(monkeypatch, mercator_spend_db=tmp_path / "nope.db", mercator_dir=tmp_path)
    r = client.get("/api/spend")
    assert r.status_code == 200
    assert r.json()["paid_inr_24h"] == 0


def test_ledger_endpoint_shape(monkeypatch, tmp_path):
    _with_config(monkeypatch, mercator_ledger_db=tmp_path / "m.db", emptor_ledger_db=tmp_path / "e.db")
    r = client.get("/api/ledger")
    body = r.json()
    assert r.status_code == 200
    assert body["events"] == []
    assert set(body["chains"]) == {"mercator", "emptor"}


# --------------------------- /api/story ---------------------------


def _seed_autopay_run(emptor_db, mercator_db):
    el = Ledger(emptor_db)
    el.log_event({"goal": "a fantasy novel for a teenager", "budget_inr": 800}, actor="emptor", event_type="goal_received")
    el.log_event({"catalog_size": 6, "affordable_count": 4}, actor="emptor", event_type="catalog_retrieved")
    el.log_event(
        {"picks": [{"product_id": "prod_001", "quantity": 1}], "reasoning": "the only in-budget book", "source": "llm"},
        actor="emptor", event_type="llm_decision",
    )
    el.log_event(
        {"ok": True, "items": [{"product_id": "prod_001", "quantity": 1, "line_total_inr": 450}], "total_inr": 450},
        actor="emptor", event_type="validation_result",
    )
    el.log_event({"total_inr": 450, "status": "paid", "settled_via": "autopay"}, actor="emptor", event_type="checkout_result")
    el.close()
    ml = Ledger(mercator_db)
    ml.log_event(
        {"idempotency_key": "idem-xyz", "outcome": "autopay_settled", "human_approval": False, "replay": False,
         "amount_inr": 450, "balance_before_inr": 550, "balance_after_inr": 100, "autopay_threshold_inr": 800,
         "autopay_allowed_categories": ["books"], "fallback_cause": None},
        actor="mercator", event_type="autopay_result",
    )
    ml.close()


def _stub_names(monkeypatch, mapping):
    async def fake_names():
        return mapping

    monkeypatch.setattr(app_module, "_catalog_names", fake_names)


def test_story_not_ready_without_a_run(monkeypatch, tmp_path):
    _with_config(monkeypatch, mercator_ledger_db=tmp_path / "m.db", emptor_ledger_db=tmp_path / "e.db")
    _stub_names(monkeypatch, {})
    r = client.get("/api/story")
    assert r.status_code == 200
    assert r.json() == {"ready": False}


def test_story_returns_facts_and_prose(monkeypatch, tmp_path):
    app_module._story_cache.clear()
    _seed_autopay_run(tmp_path / "e.db", tmp_path / "m.db")
    _with_config(monkeypatch, mercator_ledger_db=tmp_path / "m.db", emptor_ledger_db=tmp_path / "e.db")
    _stub_names(monkeypatch, {"prod_001": "The Hobbit"})

    calls = []

    async def fake_narrate(facts, llm, **kw):
        calls.append(facts)
        return "The Hobbit was bought for INR 450, settled by the shop with no human approval."

    monkeypatch.setattr(story, "narrate", fake_narrate)

    body = client.get("/api/story").json()
    assert body["ready"] is True
    assert body["narrated"] is True
    assert "Hobbit" in body["story"]
    assert body["facts"]["outcome"] == "settled"
    assert body["facts"]["amount_inr"] == 450
    assert body["facts"]["human_approval"] is False
    assert body["facts"]["items"][0]["name"] == "The Hobbit"

    # cached: a second request does not call the model again
    client.get("/api/story")
    assert len(calls) == 1


def test_story_degrades_when_narration_fails(monkeypatch, tmp_path):
    app_module._story_cache.clear()
    _seed_autopay_run(tmp_path / "e.db", tmp_path / "m.db")
    _with_config(monkeypatch, mercator_ledger_db=tmp_path / "m.db", emptor_ledger_db=tmp_path / "e.db")
    _stub_names(monkeypatch, {"prod_001": "The Hobbit"})

    async def boom(facts, llm, **kw):
        raise story.StoryError("ollama down")

    monkeypatch.setattr(story, "narrate", boom)

    body = client.get("/api/story").json()
    assert body["ready"] is True
    assert body["narrated"] is False
    assert body["story"] is None
    assert body["facts"]["amount_inr"] == 450
