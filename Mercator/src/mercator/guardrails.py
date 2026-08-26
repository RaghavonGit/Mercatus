"""Pure, deterministic guardrail checks. No async, no MCP, no Razorpay.

Each item in a cart's ``items`` list is assumed to already carry
``price_inr``/``category``/``in_stock``/``stock`` resolved from the
server-side catalog by the caller (``cart.py``, not yet built) — these
guardrails never look up a catalog themselves, they only trust the
per-item fields they are handed.
"""

from dataclasses import dataclass
from typing import Callable

from mercator import reasons
from mercator.limits import MAX_ID_LENGTH


@dataclass
class GuardrailResult:
    passed: bool
    reason: str | None = None
    detail: str | None = None


def check_spend_cap(cart_total_inr: int, cap_inr: int) -> GuardrailResult:
    if not isinstance(cap_inr, int) or isinstance(cap_inr, bool) or cap_inr <= 0:
        return GuardrailResult(False, reasons.SPEND_CAP_EXCEEDED, "No valid spend cap configured")
    if not isinstance(cart_total_inr, int) or isinstance(cart_total_inr, bool) or cart_total_inr < 0:
        return GuardrailResult(False, reasons.SPEND_CAP_EXCEEDED, "Cart total is not a valid integer amount")
    if cart_total_inr > cap_inr:
        return GuardrailResult(
            False,
            reasons.SPEND_CAP_EXCEEDED,
            f"Cart total {cart_total_inr} exceeds cap {cap_inr}",
        )
    return GuardrailResult(passed=True)


def check_input(cart: dict | None) -> GuardrailResult:
    if not isinstance(cart, dict):
        return GuardrailResult(False, reasons.CART_NOT_FOUND, "No cart provided")

    items = cart.get("items")
    if not isinstance(items, list) or not items:
        return GuardrailResult(False, reasons.INVALID_INPUT, "Cart has no items")

    for idx, item in enumerate(items):
        product_id = item.get("product_id")
        if not isinstance(product_id, str) or not product_id or len(product_id) > MAX_ID_LENGTH:
            return GuardrailResult(False, reasons.INVALID_INPUT, f"Item at index {idx}: invalid product_id")

        quantity = item.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            return GuardrailResult(False, reasons.INVALID_INPUT, f"Item at index {idx}: invalid quantity")

        price_inr = item.get("price_inr")
        if not isinstance(price_inr, int) or isinstance(price_inr, bool) or price_inr < 0:
            return GuardrailResult(False, reasons.INVALID_INPUT, f"Item at index {idx}: invalid price_inr")

    return GuardrailResult(passed=True)


def check_cart_exists(cart: dict | None) -> GuardrailResult:
    if cart is None:
        return GuardrailResult(False, reasons.CART_NOT_FOUND, "No cart provided")
    return GuardrailResult(passed=True)


def check_idempotency_key_present(idempotency_key: str | None) -> GuardrailResult:
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return GuardrailResult(False, reasons.MISSING_IDEMPOTENCY_KEY, "No idempotency key provided")
    if len(idempotency_key) > MAX_ID_LENGTH:
        return GuardrailResult(False, reasons.MISSING_IDEMPOTENCY_KEY, "Idempotency key exceeds max length")
    return GuardrailResult(passed=True)


def run_all_guardrails(
    cart: dict | None,
    idempotency_key: str | None,
    config: dict,
    log_fn: Callable[[str, GuardrailResult], None] | None = None,
) -> GuardrailResult:
    def items():
        return cart["items"]

    def categories():
        return [item["category"] for item in cart["items"]]

    def cart_total():
        return sum(item["price_inr"] * item["quantity"] for item in cart["items"])

    checks: list[tuple[str, Callable[[], GuardrailResult]]] = [
        ("check_input", lambda: check_input(cart)),
        ("check_idempotency_key_present", lambda: check_idempotency_key_present(idempotency_key)),
        ("check_cart_exists", lambda: check_cart_exists(cart)),
        ("check_stock", lambda: check_stock(items())),
        ("check_allowlist", lambda: check_allowlist(categories(), config.get("allowed_categories", []))),
        ("check_spend_cap", lambda: check_spend_cap(cart_total(), config.get("spend_cap_inr"))),
    ]

    first_failure: GuardrailResult | None = None
    for name, check in checks:
        try:
            result = check()
        except Exception as exc:
            result = GuardrailResult(False, reasons.INVALID_INPUT, f"{name} raised {type(exc).__name__}")

        try:
            if log_fn is not None:
                log_fn(name, result)
        except Exception:
            pass

        if not result.passed and first_failure is None:
            first_failure = result

    return first_failure or GuardrailResult(passed=True)


def check_stock(items: list[dict]) -> GuardrailResult:
    for idx, item in enumerate(items):
        if not item["in_stock"] or item["quantity"] > item["stock"]:
            return GuardrailResult(
                False,
                reasons.OUT_OF_STOCK,
                f"Item at index {idx} is not available in the requested quantity",
            )
    return GuardrailResult(passed=True)


def check_allowlist(categories: list[str], allowed: list[str]) -> GuardrailResult:
    if not allowed:
        return GuardrailResult(False, reasons.NOT_ALLOWLISTED, "No categories permitted")
    for idx, category in enumerate(categories):
        if category not in allowed:
            return GuardrailResult(
                False,
                reasons.NOT_ALLOWLISTED,
                f"Item at index {idx} has a category not on the allowlist",
            )
    return GuardrailResult(passed=True)
