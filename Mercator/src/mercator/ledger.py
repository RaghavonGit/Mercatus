"""Thin adapter writing Mercator's audit entries to Fides, the hash-chain
trust ledger (CLAUDE.md section 9.5). Fides is now built; every write goes
through ``fides.Ledger.log_event`` and lands in the same tamper-evident
chain Emptor also writes to.

Preserves the exact ``log``/``log_guardrail_check``/``log_checkout_result``
call shapes ``server.py`` and ``guardrails.run_all_guardrails``'s ``log_fn``
callback already depend on (Fides CLAUDE.md section 9's integration
contract: call sites "should not need to change shape, only what's
underneath them"). The one difference is the return type: ``None`` ->
``fides.LedgerEntry``, so callers (mainly tests) can inspect exactly what
was persisted without Fides needing a query API of its own (deliberately
out of scope, Fides CLAUDE.md section 10).

``actor`` is hardcoded to "mercator" -- this package only ever writes as
itself, so no call site legitimately needs to override it.
"""

from __future__ import annotations

from pathlib import Path

import fides

_ACTOR = "mercator"


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self._ledger = fides.Ledger(path)

    def log(self, event_type: str, data: dict) -> fides.LedgerEntry:
        return self._ledger.log_event(data, actor=_ACTOR, event_type=event_type)

    def log_guardrail_check(self, check_name: str, result) -> fides.LedgerEntry:
        return self.log(
            "guardrail_check",
            {
                "check": check_name,
                "passed": result.passed,
                "reason": result.reason,
                "detail": result.detail,
            },
        )

    def log_checkout_result(self, cart_id: str, idempotency_key: str, result: dict) -> fides.LedgerEntry:
        return self.log(
            "checkout_result",
            {
                "cart_id": cart_id,
                "idempotency_key": idempotency_key,
                "ok": result.get("ok"),
                "reason": result.get("reason"),
            },
        )

    def verify_chain(self) -> fides.ChainVerificationResult:
        """Passthrough for tests/ops tooling confirming the chain Mercator
        has been writing to is still intact."""
        return self._ledger.verify_chain()

    def close(self) -> None:
        self._ledger.close()
