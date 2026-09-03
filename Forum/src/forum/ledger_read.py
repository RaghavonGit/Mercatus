"""Read-only view of the two Fides ledgers the demo touches:

- Mercator's own ``ledger.db`` (actor ``mercator``) - guardrail checks,
  checkout results, the reconciler's terminal outcomes.
- Forum's Emptor-side ledger (actor ``emptor``) - goal, catalog, the LLM
  decision + reasoning, validation, the pending checkout.

Each file is an independent hash chain. Forum never writes to Mercator's
file and only appends to its own via ``fides.log_event``; here we only
read. Reading the SQLite table read-only does not disturb either chain.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fides.ledger import Ledger


def _read_rows(db_path: Path, after_seq: int) -> list[dict]:
    if not db_path.exists():
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT seq, timestamp, actor, event_type, data, entry_hash "
            "FROM ledger WHERE seq > ? ORDER BY seq ASC",
            (after_seq,),
        )
        out = []
        for r in cur:
            try:
                data = json.loads(r["data"])
            except (TypeError, ValueError):
                data = {}
            out.append(
                {
                    "seq": r["seq"],
                    "timestamp": r["timestamp"],
                    "actor": r["actor"],
                    "event_type": r["event_type"],
                    "data": data,
                    "entry_hash": r["entry_hash"][:12],
                }
            )
        return out
    finally:
        conn.close()


def _verify(db_path: Path) -> dict:
    if not db_path.exists():
        return {"present": False, "valid": None, "entries": 0}
    try:
        r = Ledger(db_path).verify_chain()
        return {
            "present": True,
            "valid": bool(r.is_valid),
            "entries": r.entries_checked,
            "broken_at_seq": r.broken_at_seq,
        }
    except Exception as exc:  # noqa: BLE001 - the demo must not crash on a read
        return {"present": True, "valid": None, "entries": 0, "error": type(exc).__name__}


def read_ledgers(
    mercator_db: Path,
    emptor_db: Path,
    after_mercator: int = 0,
    after_emptor: int = 0,
) -> dict:
    """New rows since the given per-chain cursors, plus each chain's
    verification result. The frontend keeps the two cursors and polls."""
    mercator_rows = _read_rows(mercator_db, after_mercator)
    emptor_rows = _read_rows(emptor_db, after_emptor)

    merged = sorted(
        [*mercator_rows, *emptor_rows],
        key=lambda row: (row["timestamp"], row["actor"], row["seq"]),
    )

    return {
        "events": merged,
        "chains": {
            "mercator": _verify(mercator_db),
            "emptor": _verify(emptor_db),
        },
        "cursors": {
            "mercator": max([r["seq"] for r in mercator_rows], default=after_mercator),
            "emptor": max([r["seq"] for r in emptor_rows], default=after_emptor),
        },
    }
