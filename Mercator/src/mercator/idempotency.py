"""In-memory idempotency key store.

Restarting the process loses this store — acceptable for v1 demo scope
(see README "Known limitations"). SQLite is the obvious upgrade.
"""

import copy
from typing import Callable

from mercator import reasons
from mercator.limits import MAX_ID_LENGTH, MAX_IDEMPOTENCY_ENTRIES


def is_valid_idempotency_key(key: str | None) -> bool:
    return isinstance(key, str) and bool(key.strip()) and len(key) <= MAX_ID_LENGTH


class IdempotencyStore:
    def __init__(self, max_entries: int = MAX_IDEMPOTENCY_ENTRIES) -> None:
        self._store: dict[str, dict] = {}
        self._max_entries = max_entries

    def has(self, key: str) -> bool:
        return key in self._store

    def get(self, key: str) -> dict | None:
        result = self._store.get(key)
        return copy.deepcopy(result) if result is not None else None

    def set(self, key: str, result: dict) -> None:
        self._store[key] = copy.deepcopy(result)

    def is_full(self) -> bool:
        return len(self._store) >= self._max_entries


def run_with_idempotency(
    store: IdempotencyStore,
    idempotency_key: str | None,
    operation: Callable[[], dict],
) -> dict:
    if not is_valid_idempotency_key(idempotency_key):
        return {"ok": False, "reason": reasons.MISSING_IDEMPOTENCY_KEY}

    if store.has(idempotency_key):
        return store.get(idempotency_key)

    # Fail closed on capacity *before* the operation runs: refusing a
    # brand-new key here never costs a duplicate charge, since nothing has
    # moved yet. A key already seen (the branch above) always keeps
    # replaying regardless of capacity.
    if store.is_full():
        return {"ok": False, "reason": reasons.INVALID_INPUT, "detail": "Too many pending idempotency keys"}

    result = operation()
    store.set(idempotency_key, result)
    return store.get(idempotency_key)
