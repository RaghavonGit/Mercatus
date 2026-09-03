"""The SSE orchestrator produces the right stage sequence and carries the
LLM reasoning through. Emptor's building blocks are stubbed - no real shop,
no real LLM."""

import pytest

from emptor.config import LLMSettings
from emptor.decide import DecideError, DecideResult
from emptor.purchase import PendingPurchase, PurchaseError
import forum.pipeline as pipe

LLM = LLMSettings(base_url="http://llm.test/v1", model="m", fallback_model="m2", api_key=None)
CATALOG = [
    {"id": "p1", "name": "The Hobbit", "price_inr": 450, "in_stock": True, "category": "books"},
    {"id": "p2", "name": "Fountain Pen", "price_inr": 1200, "in_stock": True, "category": "stationery"},
]
PENDING = PendingPurchase(
    payment_link_id="plink_1",
    payment_link_url="https://rzp.io/i/plink_1",
    total_inr=450,
    expire_hours=6,
)


class _FakeConn:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def __aenter__(self):
        return "session"

    async def __aexit__(self, *a):
        return None


def _wire(monkeypatch, *, decide_result=None, decide_exc=None, purchase_result=PENDING):
    monkeypatch.setattr(pipe, "ShopConnection", _FakeConn)
    monkeypatch.setattr(pipe, "_safe_log", lambda *a, **k: None)

    async def fake_discover(session):
        return CATALOG

    async def fake_decide(goal, budget, catalog, llm):
        if decide_exc:
            raise decide_exc
        return decide_result or DecideResult(
            picks=[{"product_id": "p1", "quantity": 1}], reasoning="the only in-budget book"
        )

    async def fake_purchase(session, validated):
        if isinstance(purchase_result, Exception):
            raise purchase_result
        return purchase_result

    monkeypatch.setattr(pipe, "discover_catalog", fake_discover)
    monkeypatch.setattr(pipe, "decide", fake_decide)
    monkeypatch.setattr(pipe, "purchase", fake_purchase)


async def _collect(gen):
    return [ev async for ev in gen]


async def test_happy_path_stage_sequence_and_reasoning(monkeypatch):
    _wire(monkeypatch)
    events = await _collect(pipe.run_pipeline("a fantasy novel", 800, "http://x/mcp", LLM))
    stages = [e["stage"] for e in events]
    assert stages == [
        "start", "connected", "catalog", "deciding", "decision", "validation", "pending", "done"
    ]
    decision = next(e for e in events if e["stage"] == "decision")
    assert decision["reasoning"] == "the only in-budget book"
    assert decision["picks"][0]["name"] == "The Hobbit"
    validation = next(e for e in events if e["stage"] == "validation")
    assert validation["ok"] is True
    assert all(c["ok"] for c in validation["checks"])
    pending = next(e for e in events if e["stage"] == "pending")
    assert pending["payment_link_url"] == "https://rzp.io/i/plink_1"


async def test_manual_override_skips_the_llm(monkeypatch):
    _wire(monkeypatch, decide_exc=AssertionError("decide must not run"))
    events = await _collect(
        pipe.run_pipeline(
            "x", 800, "http://x/mcp", LLM, picks_override=[{"product_id": "p1", "quantity": 1}]
        )
    )
    stages = [e["stage"] for e in events]
    assert "deciding" not in stages
    decision = next(e for e in events if e["stage"] == "decision")
    assert decision["source"] == "manual-override"
    assert "manual override" in decision["reasoning"]


async def test_decide_error_blocks_at_decide(monkeypatch):
    _wire(monkeypatch, decide_exc=DecideError("ollama down"))
    events = await _collect(pipe.run_pipeline("x", 800, "http://x/mcp", LLM))
    blocked = next(e for e in events if e["stage"] == "blocked")
    assert blocked["at"] == "decide"
    assert events[-1]["stage"] == "done"


async def test_out_of_catalog_pick_blocks_at_validate(monkeypatch):
    _wire(
        monkeypatch,
        decide_result=DecideResult(picks=[{"product_id": "nope", "quantity": 1}], reasoning="x"),
    )
    events = await _collect(pipe.run_pipeline("x", 800, "http://x/mcp", LLM))
    blocked = next(e for e in events if e["stage"] == "blocked")
    assert blocked["at"] == "validate"


async def test_purchase_error_blocks_at_purchase(monkeypatch):
    _wire(monkeypatch, purchase_result=PurchaseError("shop rejected"))
    events = await _collect(pipe.run_pipeline("x", 800, "http://x/mcp", LLM))
    blocked = next(e for e in events if e["stage"] == "blocked")
    assert blocked["at"] == "purchase"


async def test_nothing_affordable_blocks_at_filter(monkeypatch):
    _wire(monkeypatch)
    events = await _collect(pipe.run_pipeline("x", 10, "http://x/mcp", LLM))  # budget below all
    blocked = next(e for e in events if e["stage"] == "blocked")
    assert blocked["at"] == "filter"
