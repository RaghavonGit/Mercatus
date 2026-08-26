"""Fides demo script -- not part of src/fides, no installed entry point.

Logs a handful of events, verifies the chain (prints "intact"), tampers one
row via raw SQL (simulating an attacker with DB access), verifies again
(prints the exact break), then writes and prints an anchor receipt.

Run with: uv run python examples/demo.py
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fides.anchor import write_anchor
from fides.ledger import Ledger


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "demo.db"
        anchor_path = Path(tmpdir) / "demo_anchor.log"
        ledger = Ledger(db_path)

        print("--- Logging events ---")
        ledger.log_event(
            {"goal": "birthday gift for a friend who likes hiking", "budget_inr": 1500},
            actor="emptor", event_type="goal_received",
        )
        ledger.log_event({"count": 12}, actor="mercator", event_type="catalog_retrieved")
        ledger.log_event(
            {"check": "price_bound", "passed": True}, actor="mercator", event_type="guardrail_check",
        )
        last = ledger.log_event(
            {"cart_id": "cart_demo001", "ok": True}, actor="mercator", event_type="checkout_result",
        )
        print(f"logged {last.seq} events, last entry_hash={last.entry_hash}")

        print("\n--- Verifying untouched chain ---")
        result = ledger.verify_chain()
        print(f"is_valid={result.is_valid} entries_checked={result.entries_checked}")
        assert result.is_valid

        print("\n--- Simulating an attacker with DB access (raw SQL, bypassing the API) ---")
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE ledger SET data = ? WHERE seq = 2", ('{"count":999999}',))
        conn.commit()
        conn.close()
        print("tampered seq=2's data field directly")

        print("\n--- Verifying tampered chain ---")
        result = ledger.verify_chain()
        print(
            f"is_valid={result.is_valid} broken_at_seq={result.broken_at_seq} "
            f"reason={result.reason}\ndetail: {result.detail}"
        )
        assert not result.is_valid

        print("\n--- Writing external anchor (receipt of the receipt) ---")
        line = write_anchor(last.entry_hash, last.seq, path=anchor_path)
        print(f"anchor written: {line}")
        print(
            "\nNote: verify_chain() alone cannot detect wholesale replacement or "
            "truncation of the database file -- only an anchor kept outside the "
            "database, compared against the chain's current tail, can. See "
            "README.md's 'Known limitations' section."
        )

        ledger.close()


if __name__ == "__main__":
    main()
