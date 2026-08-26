from __future__ import annotations

from typing import NotRequired, TypedDict


class Product(TypedDict):
    id: str
    name: str
    price_inr: int | float
    in_stock: bool
    description: NotRequired[str]
    category: NotRequired[str]


def pre_filter(catalog: list[Product], budget_inr: int) -> list[Product]:
    return [p for p in catalog if p["in_stock"] and p["price_inr"] <= budget_inr]
