"""Public API: log_event() / verify_chain() (CLAUDE.md section 1.2).

Two surfaces are exposed:
- ``Ledger`` -- the real, testable implementation. Every test constructs its
  own isolated ``Ledger(tmp_path / "test.db")``.
- Module-level ``log_event``/``verify_chain`` -- thin wrappers bound to a
  lazily-constructed default ``Ledger`` (path from ``FIDES_DB_PATH``, else
  ``./fides_ledger.db``), so the literal ``fides.log_event(...)`` call
  CLAUDE.md sections 1.2/9 promise actually works.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fides.entry import LedgerEntry, canonical_data_json, compute_hash
from fides.store import Store


class FidesError(Exception):
    """Base class for all Fides-raised errors.

    Deliberately not a subclass of ValueError/RuntimeError: a caller's
    generic `except ValueError` must never accidentally swallow a Fides
    failure and treat it as a silent no-op (CLAUDE.md rule 3).
    """


class InvalidEntryError(FidesError):
    """Validation failed before any write was attempted. Nothing was
    written to the store."""


class LedgerWriteError(FidesError):
    """The underlying store raised a sqlite3.Error during a write."""


@dataclass(frozen=True)
class ChainVerificationResult:
    """Result of walking the chain from the start and recomputing every hash.

    ``entries_checked`` is the number of rows the walk actually examined --
    the 1-indexed position of the row that broke the chain when
    ``is_valid`` is False (not necessarily the full row count), or the full
    row count when ``is_valid`` is True.
    """

    is_valid: bool
    entries_checked: int
    broken_at_seq: int | None
    reason: str | None  # None | "entry_hash_mismatch" | "prev_hash_mismatch"
    detail: str | None  # short, never includes raw `data` content


class Ledger:
    """Owns one Store and provides the two public operations over it."""

    def __init__(self, db_path: str | Path) -> None:
        self._store = Store(db_path)

    def log_event(self, data: dict, *, actor: str, event_type: str) -> LedgerEntry:
        """Append a new hash-linked entry. Validates before writing
        anything -- a rejected entry leaves the chain exactly as it was.
        """
        if not isinstance(actor, str) or not actor.strip():
            raise InvalidEntryError("actor must be a non-empty string")
        if not isinstance(event_type, str) or not event_type.strip():
            raise InvalidEntryError("event_type must be a non-empty string")
        if not isinstance(data, dict):
            raise InvalidEntryError(f"data must be a dict, got {type(data).__name__}")

        try:
            data_json = canonical_data_json(data)
        except (TypeError, ValueError) as exc:
            raise InvalidEntryError(f"data is not JSON-serializable: {exc}") from exc

        timestamp = datetime.now(timezone.utc).isoformat()

        try:
            return self._store.append(timestamp, actor, event_type, data_json, compute_hash)
        except sqlite3.Error as exc:
            raise LedgerWriteError(str(exc)) from exc

    def verify_chain(self) -> ChainVerificationResult:
        """Walk every entry in seq order, recompute every hash from the raw
        stored fields (never a cache), report "intact" or exactly where it
        broke. Always walks the full chain from the start (CLAUDE.md section
        14 decision: v1 default, no range verification).
        """
        rows = self._store.get_all_entries()
        if not rows:
            return ChainVerificationResult(
                is_valid=True, entries_checked=0, broken_at_seq=None, reason=None,
                detail="ledger is empty",
            )

        prev_seq: int | None = None
        prev_hash: str | None = None

        for i, row in enumerate(rows, start=1):
            recomputed = compute_hash(
                row["seq"], row["timestamp"], row["actor"], row["event_type"],
                row["data"], row["prev_hash"],
            )
            if recomputed != row["entry_hash"]:
                return ChainVerificationResult(
                    is_valid=False, entries_checked=i, broken_at_seq=row["seq"],
                    reason="entry_hash_mismatch",
                    detail=(
                        f"stored entry_hash at seq={row['seq']} does not match "
                        "the hash recomputed from its stored fields"
                    ),
                )

            if prev_seq is None:
                if row["prev_hash"] is not None:
                    return ChainVerificationResult(
                        is_valid=False, entries_checked=i, broken_at_seq=row["seq"],
                        reason="prev_hash_mismatch",
                        detail=(
                            f"stored prev_hash at seq={row['seq']} should be null "
                            "for the first entry in the chain"
                        ),
                    )
            elif row["prev_hash"] != prev_hash:
                return ChainVerificationResult(
                    is_valid=False, entries_checked=i, broken_at_seq=row["seq"],
                    reason="prev_hash_mismatch",
                    detail=(
                        f"stored prev_hash at seq={row['seq']} does not match "
                        f"entry_hash of seq={prev_seq}"
                    ),
                )

            prev_seq, prev_hash = row["seq"], row["entry_hash"]

        return ChainVerificationResult(
            is_valid=True, entries_checked=len(rows), broken_at_seq=None,
            reason=None, detail=None,
        )

    def last_entry_hash(self) -> str | None:
        """The current last entry's entry_hash, read live from the store
        (never cached) -- None if the ledger is empty. This is the value a
        caller should pass to anchor.write_anchor, and what a previously
        written anchor line should be compared against to detect
        truncation or wholesale replacement (section 5's limitation)."""
        rows = self._store.get_all_entries()
        return rows[-1]["entry_hash"] if rows else None

    def close(self) -> None:
        self._store.close()


# --- Module-level default-instance wrappers ---------------------------------
#
# Guarded by their own lock (double-checked): lazy construction from two
# threads without a lock would build two Ledger objects (two Store objects,
# two locks on one file) -- exactly the failure mode the restart test in
# test_store.py exists to guard against, reintroduced through this
# convenience API that Emptor/Mercator will actually call.

_default_lock = threading.Lock()
_default_ledger: Ledger | None = None


def _get_default_ledger() -> Ledger:
    global _default_ledger
    if _default_ledger is None:
        with _default_lock:
            if _default_ledger is None:
                path = os.environ.get("FIDES_DB_PATH", "fides_ledger.db")
                _default_ledger = Ledger(path)
    return _default_ledger


def log_event(data: dict, *, actor: str, event_type: str) -> LedgerEntry:
    return _get_default_ledger().log_event(data, actor=actor, event_type=event_type)


def verify_chain() -> ChainVerificationResult:
    return _get_default_ledger().verify_chain()
