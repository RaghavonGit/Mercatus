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


def check_cumulative_spend_cap(
    already_spent_inr: int, new_total_inr: int, cumulative_cap_inr: int
) -> GuardrailResult:
    if (
        not isinstance(cumulative_cap_inr, int)
        or isinstance(cumulative_cap_inr, bool)
        or cumulative_cap_inr <= 0
    ):
        return GuardrailResult(
            False, reasons.CUMULATIVE_SPEND_CAP_EXCEEDED, "No valid cumulative spend cap configured"
        )
    if (
        not isinstance(already_spent_inr, int)
        or isinstance(already_spent_inr, bool)
        or already_spent_inr < 0
    ):
        return GuardrailResult(
            False, reasons.CUMULATIVE_SPEND_CAP_EXCEEDED, "Already-spent amount is not a valid integer amount"
        )
    if not isinstance(new_total_inr, int) or isinstance(new_total_inr, bool) or new_total_inr < 0:
        return GuardrailResult(
            False, reasons.CUMULATIVE_SPEND_CAP_EXCEEDED, "New total is not a valid integer amount"
        )
    running_total = already_spent_inr + new_total_inr
    if running_total > cumulative_cap_inr:
        return GuardrailResult(
            False,
            reasons.CUMULATIVE_SPEND_CAP_EXCEEDED,
            f"Cumulative total {running_total} exceeds cap {cumulative_cap_inr}",
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
    # Aggregate quantity per product_id before comparing to stock -- two
    # lines of the same product_id each individually <= stock can still sum
    # past it, so checking each line independently is not enough. A
    # genuinely single-line product still gets the original index-based
    # message (identical to pre-fix behavior, and avoids echoing a raw
    # client-supplied product_id into the detail on the common path); only
    # once a product spans multiple lines does an index stop identifying a
    # single failing line unambiguously, so the product_id is named instead.
    seen_order: list = []
    first_index: dict = {}
    line_count: dict = {}
    totals: dict = {}
    in_stock_flags: dict = {}
    stock_by_product: dict = {}

    for idx, item in enumerate(items):
        product_id = item.get("product_id")
        if product_id not in totals:
            seen_order.append(product_id)
            first_index[product_id] = idx
            line_count[product_id] = 0
            totals[product_id] = 0
            in_stock_flags[product_id] = True
        line_count[product_id] += 1
        totals[product_id] += item["quantity"]
        in_stock_flags[product_id] = in_stock_flags[product_id] and bool(item["in_stock"])
        # Fail closed on conflicting stock snapshots across lines for the
        # same product: take the lower one rather than last-write-wins.
        # Unreachable via cart.py today (CartStore.add_item merges same-
        # product lines rather than duplicating them, so both lines always
        # carry the same catalog snapshot) -- hardening for other callers.
        stock_by_product[product_id] = min(stock_by_product.get(product_id, item["stock"]), item["stock"])

    for product_id in seen_order:
        if not in_stock_flags[product_id] or totals[product_id] > stock_by_product[product_id]:
            detail = (
                f"Item at index {first_index[product_id]} is not available in the requested quantity"
                if line_count[product_id] == 1
                else f"Product {product_id} is not available in the requested quantity"
            )
            return GuardrailResult(False, reasons.OUT_OF_STOCK, detail)
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
