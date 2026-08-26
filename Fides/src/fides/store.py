"""SQLite storage backend.

Mirrors the locking pattern already proven correct in Mercator's
``CartStore`` (``mercator/cart.py``): a single instance-level
``threading.Lock`` wraps every public method's entire body, no separate
read lock, and no internal helper is ever independently locked (avoids
double-acquire deadlock, since ``threading.Lock`` isn't reentrant).

Rationale for one shared lock covering both reads and writes: ``append``'s
"read the last entry's hash, then write a new entry pointing at it" must be
atomic, and ``get_all_entries`` must always observe the actual persisted
state (CLAUDE.md rule 6) rather than a state a concurrent writer is
mid-way through changing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable

from fides.entry import LedgerEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    data TEXT NOT NULL,
    prev_hash TEXT,
    entry_hash TEXT NOT NULL
);
"""

EntryHashFn = Callable[..., str]


class Store:
    """Thread-safe SQLite-backed append log for ledger entries."""

    def __init__(self, db_path: str | Path) -> None:
        # check_same_thread=False: correctness is enforced by self._lock,
        # not by sqlite3's own thread-affinity check -- this store IS meant
        # to be called from multiple threads (CLAUDE.md section 6).
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def append(
        self,
        timestamp: str,
        actor: str,
        event_type: str,
        data_json: str,
        entry_hash_fn: EntryHashFn,
    ) -> LedgerEntry:
        """Append one entry inside the lock: read the last entry's hash,
        compute this entry's hash against it, insert, return the stored
        LedgerEntry. Raises the underlying sqlite3.Error uncaught on
        failure (translated to LedgerWriteError by ledger.py) -- nothing is
        left partially written, since the rollback happens before the
        exception propagates.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                next_seq = 1
                prev_hash: str | None = None
            else:
                next_seq = row["seq"] + 1
                prev_hash = row["entry_hash"]

            # Keyword call, deliberately: compute_hash takes six params,
            # four of them adjacent strings -- a positional call here would
            # let a transposed pair of arguments produce a chain that is
            # internally self-consistent (verify_chain calls the same
            # function the same wrong way) and so silently passes every
            # tamper-detection test. Keywords make that transposition
            # impossible to introduce silently.
            entry_hash = entry_hash_fn(
                seq=next_seq,
                timestamp=timestamp,
                actor=actor,
                event_type=event_type,
                data_json=data_json,
                prev_hash=prev_hash,
            )

            try:
                self._conn.execute(
                    "INSERT INTO ledger "
                    "(seq, timestamp, actor, event_type, data, prev_hash, entry_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (next_seq, timestamp, actor, event_type, data_json, prev_hash, entry_hash),
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

            # seq is supplied explicitly as an INTEGER PRIMARY KEY value
            # (not left to AUTOINCREMENT to assign): a lock bug that let two
            # threads compute the same next_seq surfaces as a loud
            # sqlite3.IntegrityError -> LedgerWriteError, never a silent
            # extra row. The primary key protects seq uniqueness only --
            # chain linearity (no forked prev_hash) is still guarded solely
            # by self._lock.
            return LedgerEntry(
                seq=next_seq,
                timestamp=timestamp,
                actor=actor,
                event_type=event_type,
                data=json.loads(data_json),
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )

    def get_all_entries(self) -> list[sqlite3.Row]:
        """Read every row, in seq order, straight from the live persisted
        store -- never from a cache (CLAUDE.md rule 6)."""
        with self._lock:
            return self._conn.execute(
                "SELECT seq, timestamp, actor, event_type, data, prev_hash, entry_hash "
                "FROM ledger ORDER BY seq ASC"
            ).fetchall()

    def close(self) -> None:
        self._conn.close()
