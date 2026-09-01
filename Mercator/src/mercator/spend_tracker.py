"""Durable cumulative spend tracker.

Unlike ``CartStore`` / ``IdempotencyStore`` / the in-flight payment-link
tracking (all deliberately in-memory), this one *must* survive a process
restart: the cumulative spend cap is a real-money safety limit, and a
restart that reset the running total to zero would silently widen it.

SQLite-backed, one row per paid checkout. Mirrors ``fides.store.Store``'s
threading idiom exactly: ``check_same_thread=False`` with correctness
enforced by an internal ``threading.Lock`` that wraps every method body,
because the MCP SDK dispatches concurrent tool calls to separate threads.
"""

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    amount_inr INTEGER NOT NULL
);
"""


class SpendTracker:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def record_paid(self, amount_inr: int, ts: float | None = None) -> None:
        if isinstance(amount_inr, bool) or not isinstance(amount_inr, int) or amount_inr < 0:
            raise ValueError("amount_inr must be a non-negative int")
        row_ts = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                "INSERT INTO spend_events (ts, amount_inr) VALUES (?, ?)",
                (row_ts, amount_inr),
            )
            self._conn.commit()

    def sum_since(self, window_hours: int, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - window_hours * 3600
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount_inr), 0) FROM spend_events WHERE ts > ?",
                (cutoff,),
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
