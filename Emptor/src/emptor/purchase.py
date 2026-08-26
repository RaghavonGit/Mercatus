from __future__ import annotations

import json
import uuid
from typing import Any

from emptor.validate import ValidationResult


class PurchaseError(RuntimeError):
    pass


def _safe(value: object, limit: int = 200) -> str:
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


def _error_text_from_result(result: Any) -> str | None:
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return _safe(block.text)
    return None


def _order_id_from_result(result: Any) -> str | None:
    if result.structured_content is not None:
        return result.structured_content.get("order_id")

    # Some shops (confirmed live against a real MCP streamable-http server)
    # serialize a plain dict return as a JSON text block instead of
    # populating structured_content - same fallback discover.py already
    # applies to the catalog.
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) != "text":
            continue
        try:
            parsed = json.loads(block.text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "order_id" in parsed:
            return parsed["order_id"]

    return None


async def purchase(session: Any, validated: ValidationResult) -> str:
    if not validated.ok:
        raise PurchaseError(f"cannot purchase: validation failed ({validated.reason})")

    for item in validated.items:
        result = await session.call_tool(
            "add_to_cart",
            {"product_id": item.product["id"], "quantity": item.quantity},
        )
        if getattr(result, "is_error", False):
            reason = _error_text_from_result(result)
            suffix = f": {reason}" if reason else ""
            raise PurchaseError(f"add_to_cart failed for {item.product['id']}{suffix}")

    idempotency_key = str(uuid.uuid4())
    checkout_result = await session.call_tool("checkout", {"idempotency_key": idempotency_key})
    if getattr(checkout_result, "is_error", False):
        reason = _error_text_from_result(checkout_result)
        suffix = f": {reason}" if reason else ""
        raise PurchaseError(f"checkout failed (idempotency_key={idempotency_key}){suffix}")

    order_id = _order_id_from_result(checkout_result)
    if not order_id:
        raise PurchaseError(
            f"checkout succeeded but shop returned no order_id (idempotency_key={idempotency_key})"
        )

    return order_id
