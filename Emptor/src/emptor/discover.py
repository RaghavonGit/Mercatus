from __future__ import annotations

import json
from typing import Any

from emptor.filters import Product

REQUIRED_FIELDS = ("id", "name", "price_inr", "in_stock")


class DiscoverError(RuntimeError):
    pass


async def discover_catalog(session: Any, tool_name: str = "list_products") -> list[Product]:
    result = await session.call_tool(tool_name)

    if getattr(result, "is_error", False):
        raise DiscoverError(f"shop's {tool_name!r} tool returned an error")

    if result.structured_content is not None:
        raw = result.structured_content
    else:
        raw = _parse_text_content(result, tool_name)

    return _validate_catalog_shape(raw)


def _parse_text_content(result: Any, tool_name: str) -> Any:
    for block in result.content:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError as exc:
                raise DiscoverError(f"shop's {tool_name!r} tool returned non-JSON text") from exc
    raise DiscoverError(
        f"shop's {tool_name!r} tool returned no structured_content and no text content"
    )


def _validate_catalog_shape(raw: Any) -> list[Product]:
    if isinstance(raw, dict) and "products" in raw:
        raw = raw["products"]

    if not isinstance(raw, list):
        raise DiscoverError(f"catalog is not a list: got {type(raw).__name__}")

    catalog: list[Product] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DiscoverError(f"catalog item {index} is not an object")
        for field in REQUIRED_FIELDS:
            if field not in item:
                raise DiscoverError(f"catalog item {index} missing required field {field!r}")

        # Validate id is a non-empty string and unique across the catalog
        if not isinstance(item["id"], str) or not item["id"]:
            raise DiscoverError(f"catalog item {index} has invalid id: {item['id']!r}")

        if item["id"] in seen_ids:
            raise DiscoverError(f"duplicate catalog id: {item['id']!r}")
        seen_ids.add(item["id"])

        # Validate name is a string
        if not isinstance(item["name"], str):
            raise DiscoverError(f"catalog item {index} has invalid name: {item['name']!r}")

        # Validate price_inr is non-negative and is not a bool
        price = item["price_inr"]
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
            raise DiscoverError(f"catalog item {index} has invalid price_inr: {price!r}")

        # Validate in_stock is a bool
        if not isinstance(item["in_stock"], bool):
            raise DiscoverError(f"catalog item {index} has invalid in_stock: {item['in_stock']!r}")

        catalog.append(item)  # type: ignore[arg-type]

    return catalog
