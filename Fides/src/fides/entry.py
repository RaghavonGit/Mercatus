"""Hashing core: the LedgerEntry shape and the canonical hash scheme.

This module owns the ONE frozen answer to "what bytes get hashed for a given
entry" (CLAUDE.md rule 5). Both write-time (store.py) and verify-time
(ledger.py) call the same two functions below, so there is no second place
where the format could drift.

Canonical serialization scheme (frozen; changing this invalidates every
previously-computed hash in any existing chain -- treat as a breaking change):

1. ``canonical_data_json(data)`` canonicalizes the caller-supplied ``data``
   dict into the exact string that gets stored in the ``data`` column:
   ``json.dumps(data, sort_keys=True, separators=(",", ":"),
   ensure_ascii=True, allow_nan=False)``.
   - ``sort_keys=True`` makes field order in the caller's dict irrelevant.
   - ``separators=(",", ":")`` removes whitespace as a source of drift.
   - ``ensure_ascii=True`` guarantees the hash input is pure ASCII
     regardless of what text encoding the TEXT column happens to use.
   - ``allow_nan=False`` rejects NaN/Infinity outright (they have no
     canonical JSON representation) -- raises ``ValueError``.
   - A key present with value ``None`` (``{"x": None}`` -> ``'{"x":null}'``)
     is NOT the same as the key being absent (``{}`` -> ``'{}'``) -- they
     canonicalize to different strings and therefore hash differently. This
     is the explicit, decided answer to "same handling of None/missing
     fields" (CLAUDE.md rule 5).

2. ``compute_hash(...)`` takes the ALREADY-canonicalized ``data_json``
   string (not the dict) and builds the outer envelope:
   ``{"seq": seq, "timestamp": timestamp, "actor": actor,
   "event_type": event_type, "data": data_json, "prev_hash": prev_hash}``,
   dumped with the same ``sort_keys=True, separators=(",", ":"),
   ensure_ascii=True``, UTF-8 encoded, then SHA256 hexdigest.

   Deliberately double-encoded: the outer envelope's "data" field holds the
   inner JSON *string*, not the parsed dict. Verification never re-parses
   ``data`` and re-serializes it -- it rehashes the exact stored TEXT column
   byte-for-byte. A parse-then-reserialize round trip is a second place
   formatting could theoretically drift (e.g. float repr differences across
   Python versions); passing the raw stored string straight through removes
   that risk. This is the literal reading of rule 6: "recompute everything
   from the raw stored bytes."
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One fully-formed, hash-linked ledger entry."""

    seq: int
    timestamp: str
    actor: str
    event_type: str
    data: dict
    prev_hash: str | None
    entry_hash: str


def canonical_data_json(data: dict) -> str:
    """Canonicalize a caller-supplied ``data`` dict to its frozen JSON form.

    Raises ``TypeError`` if ``data`` (or any nested value) isn't
    JSON-serializable, and ``ValueError`` if it contains NaN/Infinity.
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def compute_hash(
    seq: int,
    timestamp: str,
    actor: str,
    event_type: str,
    data_json: str,
    prev_hash: str | None,
) -> str:
    """Compute the SHA256 hex digest of one entry's canonical envelope.

    ``data_json`` must already be the output of ``canonical_data_json`` (or
    the exact stored ``data`` column value at verify-time) -- this function
    never parses or reserializes it.
    """
    envelope = {
        "seq": seq,
        "timestamp": timestamp,
        "actor": actor,
        "event_type": event_type,
        "data": data_json,
        "prev_hash": prev_hash,
    }
    canonical = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
