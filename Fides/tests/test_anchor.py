"""External anchor tests.

Covers CLAUDE.md section 11 checklist item:
- Anchor value written by anchor.py matches the actual last entry's hash
"""

from fides.anchor import write_anchor
from fides.ledger import Ledger


def test_anchor_matches_last_entry_hash(tmp_path):
    ledger = Ledger(tmp_path / "anchor.db")
    ledger.log_event({"a": 1}, actor="emptor", event_type="goal_received")
    ledger.log_event({"b": 2}, actor="mercator", event_type="catalog_retrieved")
    last_entry = ledger.log_event({"c": 3}, actor="mercator", event_type="checkout_result")
    ledger.close()

    anchor_path = tmp_path / "anchor.log"
    line = write_anchor(last_entry.entry_hash, last_entry.seq, path=anchor_path)

    assert f"seq={last_entry.seq}" in line
    assert f"entry_hash={last_entry.entry_hash}" in line

    written = anchor_path.read_text(encoding="utf-8")
    assert written == line + "\n"
    assert last_entry.entry_hash in written
    assert str(last_entry.seq) in written


def test_anchor_appends_multiple_lines(tmp_path):
    anchor_path = tmp_path / "anchor.log"
    write_anchor("hash1" + "0" * 59, 1, path=anchor_path)
    write_anchor("hash2" + "0" * 59, 2, path=anchor_path)

    lines = anchor_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert "seq=1" in lines[0]
    assert "seq=2" in lines[1]


def test_ledger_last_entry_hash_matches_most_recent_log_event(tmp_path):
    ledger = Ledger(tmp_path / "last_hash.db")
    assert ledger.last_entry_hash() is None  # empty ledger

    ledger.log_event({"a": 1}, actor="emptor", event_type="goal_received")
    e2 = ledger.log_event({"b": 2}, actor="mercator", event_type="checkout_result")
    assert ledger.last_entry_hash() == e2.entry_hash
    ledger.close()


def test_anchor_can_detect_truncation_when_compared_by_caller(tmp_path):
    """anchor.py doesn't detect truncation itself -- it just gives a caller
    something external to compare against. This test demonstrates that
    comparison (by entry_hash, the value that actually commits to content --
    not just seq, which a same-seq forged replacement could still match),
    since it's the whole reason section 5's anchor exists."""
    import sqlite3

    db_path = tmp_path / "trunc.db"
    ledger = Ledger(db_path)
    ledger.log_event({"a": 1}, actor="emptor", event_type="goal_received")
    last_entry = ledger.log_event({"b": 2}, actor="mercator", event_type="checkout_result")
    ledger.close()

    anchor_path = tmp_path / "anchor.log"
    write_anchor(last_entry.entry_hash, last_entry.seq, path=anchor_path)

    # Attacker truncates the chain by deleting the last row.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM ledger WHERE seq = ?", (last_entry.seq,))
    conn.commit()
    conn.close()

    ledger2 = Ledger(db_path)
    result = ledger2.verify_chain()
    current_last_hash = ledger2.last_entry_hash()
    ledger2.close()

    # verify_chain() alone reports "intact" -- the documented limitation.
    assert result.is_valid is True

    # But comparing the anchor's entry_hash against the chain's *current*
    # last entry_hash reveals the truncation -- they no longer match,
    # because the entry the anchor pointed at is gone.
    assert last_entry.entry_hash in anchor_path.read_text(encoding="utf-8")
    assert current_last_hash != last_entry.entry_hash
