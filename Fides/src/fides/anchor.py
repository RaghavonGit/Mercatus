"""External anchoring (v1, minimum) -- CLAUDE.md section 5.

The hash chain proves internal consistency: no entry was edited without
also being caught. It does NOT, by itself, prove the whole database file
wasn't replaced wholesale with a different, internally-consistent chain
from scratch, or truncated by deleting trailing rows -- a freshly-forged or
truncated chain is just as self-consistent as the real one, and
verify_chain() would report it "intact" too.

write_anchor() writes the current chain's latest entry_hash to a location
OUTSIDE the database itself -- a "receipt of the receipt." Comparing a
chain's current last entry_hash/seq against a previously-written anchor
line is what catches truncation/replacement; the anchor file itself is not
tamper-proof either (it's a plain text file), it just requires the attacker
to also control wherever the anchor was written, which is the whole point
of keeping it external to the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def write_anchor(entry_hash: str, seq: int, *, path: str | Path = "fides_anchor.log") -> str:
    """Append one anchor line for the given entry_hash/seq to `path`
    (outside the database). Returns the exact line written, without the
    trailing newline, for easy comparison in tests/logs."""
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"{timestamp} seq={seq} entry_hash={entry_hash}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line
