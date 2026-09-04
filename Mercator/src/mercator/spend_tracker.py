"""Durable cumulative spend tracker + the autopay envelope's durable state.

Unlike ``CartStore`` / ``IdempotencyStore`` / the in-flight payment-link
tracking (all deliberately in-memory), this one *must* survive a process
restart: the cumulative spend cap is a real-money safety limit, and a
restart that reset the running total to zero would silently widen it.

Three tables, all in one SQLite file:
- ``spend_events``   -- one row per paid Payment Link (unchanged).
- ``autopay_balance``-- single row holding the prepaid envelope balance in
                        integer rupees. Funded by the top-up flow, drawn
                        down by autopay purchases.
- ``autopay_debits`` -- one row per autopay purchase, keyed by
                        ``idempotency_key``. The ``PRIMARY KEY`` *is* the
                        idempotency guarantee for the autopay path: the
                        in-memory ``IdempotencyStore`` is lost on restart,
                        but a durable debit must never replay into a second
                        charge, so the claim lives here, in the same
                        transaction as the balance decrement.

``sum_since`` spans ``spend_events`` + ``autopay_debits`` -- money out the
door via autopay counts toward ``CUMULATIVE_SPEND_CAP_INR`` exactly like a
paid Payment Link does.

Mirrors ``fides.store.Store``'s threading idiom exactly:
``check_same_thread=False`` with correctness enforced by an internal
``threading.Lock`` that wraps every method body, because the MCP SDK
dispatches concurrent tool calls to separate threads.
"""

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    amount_inr INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS autopay_balance (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance_inr INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO autopay_balance (id, balance_inr) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS autopay_debits (
    idempotency_key TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    amount_inr INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class AutopayDebitResult:
    """Outcome of ``try_autopay_debit``.

    - ``committed``    -- the debit landed; ``balance_before/after`` bracket it.
    - ``replayed``     -- this ``idempotency_key`` already debited (a restart
                          lost the in-memory result, or a genuine retry).
                          ``amount_inr`` is the *original* debit amount so the
                          caller can reconstruct the success result. No money
                          moved on this call.
    - ``insufficient`` -- balance did not cover the amount; nothing was
                          written. ``balance_before == balance_after``.
    """

    status: str
    amount_inr: int
    balance_before_inr: int
    balance_after_inr: int


def _require_nonneg_int(amount_inr: object, label: str) -> int:
    if isinstance(amount_inr, bool) or not isinstance(amount_inr, int) or amount_inr < 0:
        raise ValueError(f"{label} must be a non-negative int")
    return amount_inr


def _require_positive_int(amount_inr: object, label: str) -> int:
    if isinstance(amount_inr, bool) or not isinstance(amount_inr, int) or amount_inr <= 0:
        raise ValueError(f"{label} must be a positive int")
    return amount_inr


class SpendTracker:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def record_paid(self, amount_inr: int, ts: float | None = None) -> None:
        _require_nonneg_int(amount_inr, "amount_inr")
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
            paid = self._conn.execute(
                "SELECT COALESCE(SUM(amount_inr), 0) FROM spend_events WHERE ts > ?",
                (cutoff,),
            ).fetchone()[0]
            autopay = self._conn.execute(
                "SELECT COALESCE(SUM(amount_inr), 0) FROM autopay_debits WHERE ts > ?",
                (cutoff,),
            ).fetchone()[0]
        return int(paid) + int(autopay)

    # --- autopay envelope ------------------------------------------------

    def autopay_balance(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT balance_inr FROM autopay_balance WHERE id = 1"
            ).fetchone()
        return int(row[0])

    def credit_balance(self, amount_inr: int) -> int:
        """Add to the prepaid envelope (the top-up flow). Returns the new
        balance. The ``AUTOPAY_MAX_BALANCE_INR`` ceiling is enforced by the
        top-up caller, not here -- this method only ever moves the balance up."""
        _require_nonneg_int(amount_inr, "amount_inr")
        with self._lock:
            self._conn.execute(
                "UPDATE autopay_balance SET balance_inr = balance_inr + ? WHERE id = 1",
                (amount_inr,),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT balance_inr FROM autopay_balance WHERE id = 1"
            ).fetchone()
        return int(row[0])

    def try_autopay_debit(
        self, idempotency_key: str, amount_inr: int, ts: float | None = None
    ) -> AutopayDebitResult:
        """Atomically: claim ``idempotency_key`` and decrement the balance by
        ``amount_inr`` -- or do nothing. One transaction; the ``threading.Lock``
        serialises threads on the shared connection and the ``autopay_debits``
        PRIMARY KEY is the durable backstop. Never overdraws, never
        double-debits a key."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        _require_positive_int(amount_inr, "amount_inr")
        row_ts = time.time() if ts is None else ts

        with self._lock:
            balance_before = int(
                self._conn.execute(
                    "SELECT balance_inr FROM autopay_balance WHERE id = 1"
                ).fetchone()[0]
            )
            try:
                self._conn.execute(
                    "INSERT INTO autopay_debits (idempotency_key, ts, amount_inr) VALUES (?, ?, ?)",
                    (idempotency_key, row_ts, amount_inr),
                )
            except sqlite3.IntegrityError:
                # Key already debited -- a restart lost the in-memory result,
                # or a genuine retry. Return the stored amount; no money moves.
                self._conn.rollback()
                stored = self._conn.execute(
                    "SELECT amount_inr FROM autopay_debits WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                return AutopayDebitResult(
                    "replayed", int(stored[0]), balance_before, balance_before
                )

            updated = self._conn.execute(
                "UPDATE autopay_balance SET balance_inr = balance_inr - ? "
                "WHERE id = 1 AND balance_inr >= ?",
                (amount_inr, amount_inr),
            )
            if updated.rowcount != 1:
                # Balance does not cover it. Roll the debit-claim INSERT back
                # too, so the key stays retryable after a top-up.
                self._conn.rollback()
                return AutopayDebitResult(
                    "insufficient", amount_inr, balance_before, balance_before
                )

            self._conn.commit()
            return AutopayDebitResult(
                "committed", amount_inr, balance_before, balance_before - amount_inr
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
