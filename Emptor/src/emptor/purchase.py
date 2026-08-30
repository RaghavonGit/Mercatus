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


def _payload_from_result(result: Any) -> dict | None:
    """Returns the tool's returned dict, from structured_content if
    populated, else parsed out of a JSON text block - some shops (confirmed
    live against a real MCP streamable-http server) serialize a plain dict
    return as text content instead, same fallback discover.py already
    applies to the catalog."""
    if isinstance(result.structured_content, dict):
        return result.structured_content

    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) != "text":
            continue
        try:
            parsed = json.loads(block.text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return None


def _order_id_from_result(result: Any) -> str | None:
    payload = _payload_from_result(result)
    return payload.get("order_id") if payload else None


def _cart_id_from_result(result: Any) -> str | None:
    payload = _payload_from_result(result)
    return payload.get("cart_id") if payload else None


def _rejection_from_result(result: Any) -> tuple[str | None, str | None] | None:
    """Some shops (Mercator's real contract, CLAUDE.md section 3.1) return a
    business-logic rejection as ``{"ok": false, "reason": ..., "detail":
    ...}`` inside an otherwise-successful MCP call, deliberately never as a
    protocol-level error - so ``is_error`` alone can't detect this. Returns
    (reason, detail) if the payload explicitly says ok=false, else None."""
    payload = _payload_from_result(result)
    if isinstance(payload, dict) and payload.get("ok") is False:
        return payload.get("reason"), payload.get("detail")
    return None


def _describe_rejection(reason: str | None, detail: str | None) -> str:
    text = reason or "rejected"
    if detail:
        text += f" ({detail})"
    return text


async def purchase(session: Any, validated: ValidationResult) -> str:
    if not validated.ok:
        raise PurchaseError(f"cannot purchase: validation failed ({validated.reason})")

    cart_ids: list[str] = []
    for item in validated.items:
        add_result = await session.call_tool(
            "add_to_cart",
            {"product_id": item.product["id"], "quantity": item.quantity},
        )
        if getattr(add_result, "is_error", False):
            reason = _error_text_from_result(add_result)
            suffix = f": {reason}" if reason else ""
            raise PurchaseError(f"add_to_cart failed for {item.product['id']}{suffix}")

        rejection = _rejection_from_result(add_result)
        if rejection is not None:
            raise PurchaseError(
                f"add_to_cart failed for {item.product['id']}: "
                f"{_describe_rejection(*rejection)}"
            )

        cart_id = _cart_id_from_result(add_result)
        if cart_id is not None:
            cart_ids.append(cart_id)

    # Shops with no cart_id concept (e.g. tests/stub_shop/server.py, which
    # tracks one global cart) leave cart_ids empty - preserve the original
    # single checkout({"idempotency_key": ...}) call shape for them exactly.
    #
    # Shops that DO return a cart_id per add_to_cart call (Mercator's real
    # v1 contract, CLAUDE.md section 3: one fresh single-item cart per call)
    # have no way to check out more than one such cart in a single call -
    # Emptor's own pipeline (section 3) assumes one checkout call closes out
    # the whole pick list, which only holds when every item landed in the
    # same cart_id. >1 distinct cart_id means that assumption doesn't hold
    # against this shop; fail loudly per rule #7 rather than guessing (e.g.
    # silently checking out only one cart, or issuing N separate orders
    # under one "success" message that never promised that shape).
    distinct_cart_ids = set(cart_ids)
    if len(distinct_cart_ids) > 1:
        raise PurchaseError(
            "cannot purchase: this shop returned a separate cart_id per item "
            f"({len(distinct_cart_ids)} carts for {len(validated.items)} items) and "
            "this pipeline has no way to check out more than one cart in a single "
            "order - unresolved design gap between Emptor's multi-item pipeline and "
            "this shop's single-item-cart model, see CLAUDE.md"
        )

    idempotency_key = str(uuid.uuid4())
    checkout_args: dict[str, Any] = {"idempotency_key": idempotency_key}
    if distinct_cart_ids:
        (cart_id,) = distinct_cart_ids
        checkout_args["cart_id"] = cart_id
    checkout_result = await session.call_tool("checkout", checkout_args)
    if getattr(checkout_result, "is_error", False):
        reason = _error_text_from_result(checkout_result)
        suffix = f": {reason}" if reason else ""
        raise PurchaseError(f"checkout failed (idempotency_key={idempotency_key}){suffix}")

    rejection = _rejection_from_result(checkout_result)
    if rejection is not None:
        raise PurchaseError(
            f"checkout blocked (idempotency_key={idempotency_key}): "
            f"{_describe_rejection(*rejection)}"
        )

    order_id = _order_id_from_result(checkout_result)
    if not order_id:
        raise PurchaseError(
            f"checkout succeeded but shop returned no order_id (idempotency_key={idempotency_key})"
        )

    return order_id
