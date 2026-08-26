"""The actual demo scenario, as real automated tests: write a chain, mutate
one stored entry via raw SQL through a *separate* connection (simulating an
attacker with DB access, bypassing the Fides API entirely), call
verify_chain(), assert it correctly reports the exact broken seq.

Covers CLAUDE.md section 11 checklist items:
- Directly mutating one stored entry (raw SQL, bypassing the API) ->
  verify_chain() correctly identifies the exact seq that broke
- Mutating the last entry only breaks that one entry, not the whole chain
  before it -- report correctly localizes the break
"""

import sqlite3

import pytest

from fides.entry import compute_hash
from fides.ledger import Ledger


@pytest.fixture
def populated_ledger(tmp_path):
    db_path = tmp_path / "tamper.db"
    lg = Ledger(db_path)
    for i in range(5):
        lg.log_event({"i": i}, actor="emptor", event_type="goal_received")
    yield lg, db_path
    lg.close()


def _attacker_execute(db_path, sql, params=()):
    """Open a fresh connection to issue raw SQL, simulating an attacker with
    DB access rather than reaching into the Ledger's own connection (which
    would leave ambiguous uncommitted transaction state for the subsequent
    verify_chain() read)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def test_untampered_chain_verifies(populated_ledger):
    ledger, _ = populated_ledger
    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 5


def test_tampered_middle_entry_data_detected(populated_ledger):
    ledger, db_path = populated_ledger
    _attacker_execute(db_path, "UPDATE ledger SET data = ? WHERE seq = 3", ('{"i":999}',))

    result = ledger.verify_chain()
    assert result.is_valid is False
    assert result.broken_at_seq == 3
    assert result.reason == "entry_hash_mismatch"
    # entries_checked reflects how far the walk got, not the full row count --
    # the walk stops at the first break (seq=3 is the 3rd row examined).
    assert result.entries_checked == 3
    assert "seq=3" in result.detail
    assert "999" not in result.detail  # never dump raw data content (#17)


def test_tampered_last_entry_localizes_correctly(populated_ledger):
    """Mutating only the last entry must not be reported as "whole chain
    broken" -- the break is localized to exactly that seq, and since it's
    the last row, entries_checked equals the full row count."""
    ledger, db_path = populated_ledger
    _attacker_execute(db_path, "UPDATE ledger SET data = ? WHERE seq = 5", ('{"i":999}',))

    result = ledger.verify_chain()
    assert result.is_valid is False
    assert result.broken_at_seq == 5
    assert result.reason == "entry_hash_mismatch"
    assert result.entries_checked == 5


def _forge_self_consistent_prev_hash(db_path, seq: int, fake_prev_hash: str) -> None:
    """Simulate a more sophisticated attacker: not just editing prev_hash,
    but recomputing entry_hash to match it, so the row's own entry_hash
    check passes and only the *link* to the actual previous row's hash is
    wrong. Without this, entry_hash_mismatch fires first (entry_hash
    commits to prev_hash too), and prev_hash_mismatch would never be
    reachable as a distinct code path."""
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT timestamp, actor, event_type, data FROM ledger WHERE seq = ?", (seq,)
    ).fetchone()
    timestamp, actor, event_type, data_json = row
    forged_hash = compute_hash(seq, timestamp, actor, event_type, data_json, fake_prev_hash)
    conn.execute(
        "UPDATE ledger SET prev_hash = ?, entry_hash = ? WHERE seq = ?",
        (fake_prev_hash, forged_hash, seq),
    )
    conn.commit()
    conn.close()


def test_forged_prev_hash_link_detected(populated_ledger):
    """A self-consistent row (entry_hash matches its own stored prev_hash)
    that points at the wrong predecessor must still be caught -- distinct
    from entry_hash_mismatch, which fires only when a row's own hash
    doesn't match its own stored fields."""
    ledger, db_path = populated_ledger
    _forge_self_consistent_prev_hash(db_path, seq=3, fake_prev_hash="0" * 64)

    result = ledger.verify_chain()
    assert result.is_valid is False
    assert result.broken_at_seq == 3
    assert result.reason == "prev_hash_mismatch"
    assert "seq=3" in result.detail


def test_forged_first_entry_prev_hash_detected(populated_ledger):
    """An attacker forging seq=1's prev_hash to a fake predecessor (and
    recomputing entry_hash to match) must still be caught, even though
    there's no real predecessor row to compare against -- the walk's
    special-cased "first row must have prev_hash=None" check catches it."""
    ledger, db_path = populated_ledger
    _forge_self_consistent_prev_hash(db_path, seq=1, fake_prev_hash="deadbeef" * 8)

    result = ledger.verify_chain()
    assert result.is_valid is False
    assert result.broken_at_seq == 1
    assert result.reason == "prev_hash_mismatch"
    assert "seq=1" in result.detail


def test_naive_prev_hash_only_edit_is_actually_caught_as_entry_hash_mismatch(populated_ledger):
    """Documents a real property of the design: entry_hash commits to
    prev_hash too, so a naive attacker who edits prev_hash WITHOUT
    recomputing entry_hash is caught by the entry_hash check, not the
    prev_hash check -- either way, verify_chain() still reports the exact
    broken seq. This is not a gap; it's why entry_hash_mismatch is checked
    before prev_hash_mismatch (CLAUDE.md decision log)."""
    ledger, db_path = populated_ledger
    _attacker_execute(db_path, "UPDATE ledger SET prev_hash = ? WHERE seq = 3", ("0" * 64,))

    result = ledger.verify_chain()
    assert result.is_valid is False
    assert result.broken_at_seq == 3
    assert result.reason == "entry_hash_mismatch"


def test_deleted_middle_entry_detected_via_prev_hash_mismatch(populated_ledger):
    """Deleting a whole row (not just editing a field) breaks the link
    between its neighbors -- caught because verify_chain compares prev_hash
    against the *actual previous row encountered in the walk*, not seq-1
    arithmetic."""
    ledger, db_path = populated_ledger
    _attacker_execute(db_path, "DELETE FROM ledger WHERE seq = 3")

    result = ledger.verify_chain()
    assert result.is_valid is False
    assert result.reason == "prev_hash_mismatch"
    assert result.broken_at_seq == 4  # the row immediately after the gap


def test_deleted_trailing_entry_is_the_documented_truncation_limitation(populated_ledger):
    """Deleting the LAST entry leaves a fully self-consistent, shorter chain
    -- verify_chain() correctly reports it as intact. This is not a bug: it
    is the exact limitation CLAUDE.md section 5 and the README document
    (only an external anchor can catch truncation/wholesale replacement,
    since a hash chain alone only proves internal consistency)."""
    ledger, db_path = populated_ledger
    _attacker_execute(db_path, "DELETE FROM ledger WHERE seq = 5")

    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 4
