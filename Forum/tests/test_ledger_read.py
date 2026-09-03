from fides.ledger import Ledger

from forum.ledger_read import read_ledgers


def test_reads_and_merges_both_chains_and_verifies(tmp_path):
    merc = tmp_path / "ledger.db"
    empt = tmp_path / "forum_ledger.db"

    ml = Ledger(merc)
    ml.log_event({"check": "check_stock", "passed": True}, actor="mercator", event_type="guardrail_check")
    ml.close()

    el = Ledger(empt)
    el.log_event({"goal": "a book", "budget_inr": 800}, actor="emptor", event_type="goal_received")
    el.log_event(
        {"picks": [{"product_id": "p1", "quantity": 1}], "reasoning": "fits", "source": "llm"},
        actor="emptor",
        event_type="llm_decision",
    )
    el.close()

    out = read_ledgers(merc, empt)
    assert out["chains"]["mercator"]["valid"] is True
    assert out["chains"]["emptor"]["valid"] is True
    assert out["chains"]["emptor"]["entries"] == 2
    types = [(e["actor"], e["event_type"]) for e in out["events"]]
    assert ("mercator", "guardrail_check") in types
    assert ("emptor", "llm_decision") in types
    assert out["cursors"]["emptor"] == 2


def test_after_cursor_returns_only_new_rows(tmp_path):
    empt = tmp_path / "forum_ledger.db"
    el = Ledger(empt)
    el.log_event({"goal": "one"}, actor="emptor", event_type="goal_received")
    el.log_event({"goal": "two"}, actor="emptor", event_type="goal_received")
    el.close()

    out = read_ledgers(tmp_path / "missing.db", empt, after_emptor=1)
    assert len(out["events"]) == 1
    assert out["events"][0]["seq"] == 2
    assert out["chains"]["mercator"]["present"] is False


def test_missing_files_do_not_raise(tmp_path):
    out = read_ledgers(tmp_path / "a.db", tmp_path / "b.db")
    assert out["events"] == []
    assert out["chains"]["mercator"]["present"] is False
