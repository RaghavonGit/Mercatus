from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from emptor.filters import Product

MAX_DISTINCT_ITEMS = 10
MAX_QTY_PER_ITEM = 10


class Pick(TypedDict):
    product_id: str
    quantity: int


@dataclass(frozen=True)
class ValidatedItem:
    product: Product
    quantity: int
    line_total_inr: int


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    items: list[ValidatedItem]
    total_inr: int
    reason: str | None


def _safe(value: object, limit: int = 200) -> str:
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


def _rejected(reason: str, total_inr: int = 0) -> ValidationResult:
    return ValidationResult(ok=False, items=[], total_inr=total_inr, reason=reason)


def validate_picks(picks: list[Pick], catalog: list[Product], budget_inr: int) -> ValidationResult:
    if not picks:
        return _rejected("empty pick list")

    if len(picks) > MAX_DISTINCT_ITEMS:
        return _rejected(f"too many distinct items: {len(picks)} > {MAX_DISTINCT_ITEMS}")

    catalog_by_id = {p["id"]: p for p in catalog}
    items: list[ValidatedItem] = []
    seen_ids: set[str] = set()
    total_inr = 0

    for pick in picks:
        if not isinstance(pick, dict):
            return _rejected(f"pick is not an object: {_safe(pick)}")

        product_id = pick.get("product_id")
        quantity = pick.get("quantity")

        if not isinstance(product_id, str):
            return _rejected(f"invalid product_id: {_safe(product_id)}")

        if product_id in seen_ids:
            return _rejected(f"duplicate product_id in picks: {product_id}")
        seen_ids.add(product_id)

        if product_id not in catalog_by_id:
            return _rejected(f"picked product_id not in catalog: {_safe(product_id)}")

        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return _rejected(f"invalid quantity for {product_id}: {_safe(quantity)}")

        if quantity > MAX_QTY_PER_ITEM:
            return _rejected(f"quantity exceeds cap for {product_id}: {quantity} > {MAX_QTY_PER_ITEM}")

        product = catalog_by_id[product_id]
        line_total_inr = product["price_inr"] * quantity
        total_inr += line_total_inr
        items.append(ValidatedItem(product=product, quantity=quantity, line_total_inr=line_total_inr))

    if total_inr > budget_inr:
        return _rejected(f"total {total_inr} exceeds budget {budget_inr}", total_inr=total_inr)

    return ValidationResult(ok=True, items=items, total_inr=total_inr, reason=None)
