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
        # Persist the full money-path picture, not just ok/reason: on the
        # tamper-evident ledger a paid, cancelled, expired, partially_paid,
        # and a gateway failure must be distinguishable, and an outcome must
        # be tied to its link and amount. (Was RAZORPAY_MODE=live blocker (a).)
        return self.log(
            "checkout_result",
            {
                "cart_id": cart_id,
                "idempotency_key": idempotency_key,
                "ok": result.get("ok"),
                "reason": result.get("reason"),
                "detail": result.get("detail"),
                "status": result.get("status"),
                "payment_link_id": result.get("payment_link_id"),
                "amount": result.get("amount"),
            },
        )

    def log_autopay_result(
        self,
        cart_id: str,
        idempotency_key: str,
        *,
        outcome: str,
        amount_inr: int,
        autopay_threshold_inr: int,
        autopay_allowed_categories: list[str],
        balance_before_inr: int | None = None,
        balance_after_inr: int | None = None,
        fallback_cause: str | None = None,
        replay: bool = False,
    ) -> fides.LedgerEntry:
        """The autopay decision, always logged whether autopay was taken or
        not. ``human_approval`` is always ``False`` -- at the moment of this
        event no human has approved anything; ``outcome`` says whether money
        moved autonomously (``autopay_settled``) or a human-paid link was
        issued instead (``fell_back_to_manual``). ``which rule permitted it``
        is captured by snapshotting the threshold + allowlist in force.

        ``replay=True`` (with ``balance_*`` left ``None``) marks a settled
        outcome where this call moved no money -- the debit for this
        idempotency key had already committed on an earlier run -- so a
        reader never mistakes ``balance_before == balance_after`` for a
        zero-value autonomous charge."""
        return self.log(
            "autopay_result",
            {
                "cart_id": cart_id,
                "idempotency_key": idempotency_key,
                "outcome": outcome,
                "human_approval": False,
                "replay": replay,
                "amount_inr": amount_inr,
                "balance_before_inr": balance_before_inr,
                "balance_after_inr": balance_after_inr,
                "autopay_threshold_inr": autopay_threshold_inr,
                "autopay_allowed_categories": list(autopay_allowed_categories),
                "fallback_cause": fallback_cause,
            },
        )

    def verify_chain(self) -> fides.ChainVerificationResult:
        """Passthrough for tests/ops tooling confirming the chain Mercator
        has been writing to is still intact."""
        return self._ledger.verify_chain()

    def close(self) -> None:
        self._ledger.close()
