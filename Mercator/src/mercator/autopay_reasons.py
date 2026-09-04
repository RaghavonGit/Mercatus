"""Fixed fallback-cause codes for the autopay (tiered-autonomy) path.

Zero imports, constants only -- same pattern as ``reasons.py``.

These are NOT rejection reasons. When autopay is not taken, ``checkout``
still returns ``ok: true`` with a payable Payment Link; the cause here
records *why the human path was used* and is surfaced in the
``autopay_result`` ledger event and the optional ``autopay_fallback_cause``
field on the successful result. The hard blocks that actually reject a
checkout stay in ``reasons.py`` and fire before autopay is ever considered.
"""

# Decided by the pure guardrail (guardrails.check_autopay_eligible):
AUTOPAY_DISABLED = "AUTOPAY_DISABLED"
AUTOPAY_OVER_THRESHOLD = "AUTOPAY_OVER_THRESHOLD"
AUTOPAY_CATEGORY_NOT_ELIGIBLE = "AUTOPAY_CATEGORY_NOT_ELIGIBLE"
AUTOPAY_MULTI_ITEM = "AUTOPAY_MULTI_ITEM"

# Decided at the server, wiring the debit:
AUTOPAY_BALANCE_INSUFFICIENT = "AUTOPAY_BALANCE_INSUFFICIENT"
AUTOPAY_INTERNAL_ERROR = "AUTOPAY_INTERNAL_ERROR"
