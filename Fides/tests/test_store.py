"""Storage-layer tests: persistence, concurrency, injection safety, restart.

Covers CLAUDE.md section 11 checklist items:
- Two threads calling into the store concurrently -> no forked prev_hash
- SQL injection attempt in `data` -> stored safely, table still exists
- Restarting the process (fresh Store on the same .db file) -> data survives
"""

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from fides.entry import canonical_data_json, compute_hash
from fides.store import Store

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _append(store: Store, actor: str, event_type: str, data: dict, timestamp: str = "2026-08-26T00:00:00+00:00"):
    return store.append(timestamp, actor, event_type, canonical_data_json(data), compute_hash)


def test_basic_persistence_and_contiguous_seq(store):
    e1 = _append(store, "emptor", "goal_received", {"goal": "gift"})
    e2 = _append(store, "mercator", "catalog_retrieved", {"count": 5})
    e3 = _append(store, "mercator", "checkout_result", {"ok": True})

    assert [e1.seq, e2.seq, e3.seq] == [1, 2, 3]
    assert e1.prev_hash is None
    assert e2.prev_hash == e1.entry_hash
    assert e3.prev_hash == e2.entry_hash

    rows = store.get_all_entries()
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["entry_hash"] for r in rows] == [e1.entry_hash, e2.entry_hash, e3.entry_hash]


def test_store_speaks_the_frozen_canonical_format(store):
    """The store must call entry_hash_fn with the *same* argument binding
    entry.py's own tests exercise directly. A positional-argument bug in
    store.append (e.g. transposed actor/event_type) would still produce an
    internally self-consistent chain -- this test catches that class of bug
    by checking the stored hash against the independently-frozen fixture
    literal, not against a value store.py just computed.
    """
    fixture = json.loads((FIXTURES_DIR / "sample_entries.json").read_text())[0]
    assert fixture["seq"] == 1 and fixture["prev_hash"] is None

    entry = store.append(
        fixture["timestamp"],
        fixture["actor"],
        fixture["event_type"],
        canonical_data_json(fixture["data"]),
        compute_hash,
    )
    assert entry.entry_hash == fixture["entry_hash"]


def test_returned_entry_data_round_trips_through_json(store):
    data = {"nested": {"a": [1, 2, 3]}, "note": "hello"}
    entry = _append(store, "emptor", "goal_received", data)
    assert entry.data == data


def test_sql_injection_attempt_stored_safely(store):
    payload = {"note": "'; DROP TABLE ledger; --"}
    _append(store, "emptor", "goal_received", payload)

    rows = store.get_all_entries()
    assert len(rows) == 1
    assert json.loads(rows[0]["data"]) == payload

    # table must still exist and be queryable
    more_rows = store.get_all_entries()
    assert len(more_rows) == 1


def test_restart_reads_previously_written_entries(tmp_path):
    db_path = tmp_path / "restart.db"

    s1 = Store(db_path)
    e1 = _append(s1, "emptor", "goal_received", {"goal": "gift"})
    e2 = _append(s1, "mercator", "checkout_result", {"ok": True})
    s1.close()  # must close before reopening -- two live Store objects on
    # one file means two independent locks, which would silently invalidate
    # the concurrency guarantee if it ever leaked into another test.

    s2 = Store(db_path)
    rows = s2.get_all_entries()
    s2.close()

    assert [r["seq"] for r in rows] == [1, 2]
    assert [r["entry_hash"] for r in rows] == [e1.entry_hash, e2.entry_hash]


# --- Concurrency -----------------------------------------------------------
#
# Mercator's own postmortem (finished_mercator.md section 3.2) proved
# concurrent dispatch was real with a throwaway probe *before* trusting a
# lock fix was needed. The naive test below is that same discipline made
# durable: it proves the harness is capable of producing a forked chain at
# all, so the real (locked) test below it is proof of something, not a
# no-op that would have passed even with a broken lock.


class _NaiveUnlockedStore:
    """Deliberately missing store.Store's lock, to prove the race is real.

    Each thread gets its own sqlite3 connection (sharing one connection
    across unlocked threads produces ProgrammingError/transaction-state
    noise instead of a clean fork, which would prove nothing). SQLite's own
    file lock still serializes the two INSERTs, but both threads' SELECTs
    happen before either INSERT, so both compute prev_hash from the same
    (empty) last row -- the fork happens at the read, not the write.
    """

    def __init__(self, db_path):
        self._db_path = db_path
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE ledger (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                data TEXT NOT NULL,
                prev_hash TEXT,
                entry_hash TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def append_unlocked(self, actor, event_type, data_json, barrier):
        conn = sqlite3.connect(str(self._db_path))
        try:
            row = conn.execute(
                "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row[0] if row else None

            barrier.wait()  # force both threads past the read before either writes

            entry_hash = compute_hash(0, "t", actor, event_type, data_json, prev_hash)
            conn.execute(
                "INSERT INTO ledger (timestamp, actor, event_type, data, prev_hash, entry_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("t", actor, event_type, data_json, prev_hash, entry_hash),
            )
            conn.commit()
        finally:
            conn.close()
        return prev_hash


def test_naive_unlocked_store_actually_forks(tmp_path):
    """Proves the race harness is real: without a lock, two threads racing
    to append really do produce two rows with the same prev_hash."""
    naive = _NaiveUnlockedStore(tmp_path / "naive.db")
    barrier = threading.Barrier(2)
    results = []

    def worker(n):
        results.append(
            naive.append_unlocked("emptor", "goal_received", canonical_data_json({"n": n}), barrier)
        )

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert len(set(results)) < len(results), (
        "expected the naive unlocked store to fork (both threads read the "
        "same prev_hash) -- if this fails, the race harness itself isn't "
        "proving anything and the locked test below isn't proof of a fix"
    )


def test_store_append_is_safe_under_real_concurrency(tmp_path):
    """The real test: Store.append, raced via threading.Thread + Barrier
    (not sequential calls that happen to run in the same process). Asserts
    the resulting chain has no forked prev_hash and every stored entry_hash
    matches recomputation from that row's own stored fields.
    """
    store = Store(tmp_path / "concurrent.db")
    n_threads = 8
    barrier = threading.Barrier(n_threads)

    def worker(n):
        barrier.wait()
        _append(store, "emptor", "goal_received", {"n": n}, timestamp=f"2026-08-26T00:00:{n:02d}+00:00")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = store.get_all_entries()
    store.close()

    assert len(rows) == n_threads
    seqs = sorted(r["seq"] for r in rows)
    assert seqs == list(range(1, n_threads + 1)), "seq must be contiguous, no gaps or dupes"

    prev_hashes = [r["prev_hash"] for r in rows]
    assert len(set(prev_hashes)) == len(prev_hashes), "no two entries may share a prev_hash (forked chain)"

    for row in rows:
        recomputed = compute_hash(
            row["seq"], row["timestamp"], row["actor"], row["event_type"], row["data"], row["prev_hash"]
        )
        assert recomputed == row["entry_hash"]
