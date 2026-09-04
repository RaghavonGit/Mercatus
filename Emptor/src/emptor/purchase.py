from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from emptor.validate import ValidationResult


class PurchaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingPurchase:
    """What ``purchase()`` returns now: a payment link the buyer still has
    to pay. Deliberately NOT called/typed as a completed order -- no money
    has moved when this is returned. Mercator's in-process reconciler logs
    the real final outcome later, from its own process."""

    payment_link_id: str
    payment_link_url: str
    total_inr: int
    expire_hours: int | None


@dataclass(frozen=True)
class SettledPurchase:
    """The other successful outcome: the shop settled the checkout
    immediately with **no payment link to pay** -- it drew a pre-funded
    balance it holds (Mercator's autopay envelope). Money HAS moved. Emptor
    does not control or authorize this -- the shop decides, deterministically,
    server-side; Emptor only reports what came back. ``settled_via`` is
    shop-supplied text, display-only."""

    total_inr: int
    settled_via: str
    amount_inr: int | None


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


def _payment_link_from_result(result: Any) -> tuple[str, str, int | None] | None:
    """Mercator's post-migration checkout contract: a successful checkout
    returns a payment link (id + payable URL), never an order_id -- money
    hasn't moved yet. Returns (payment_link_id, payment_link_url,
    expire_hours) or None if either required field is absent/blank."""
    payload = _payload_from_result(result)
    if not payload:
        return None
    link_id = payload.get("payment_link_id")
    link_url = payload.get("payment_link_url")
    if not isinstance(link_id, str) or not link_id:
        return None
    if not isinstance(link_url, str) or not link_url:
        return None
    expire_hours = payload.get("expire_hours")
    if not isinstance(expire_hours, int) or isinstance(expire_hours, bool):
        expire_hours = None
    return link_id, link_url, expire_hours


def _cart_id_from_result(result: Any) -> str | None:
    payload = _payload_from_result(result)
    return payload.get("cart_id") if payload else None


def _settled_from_result(result: Any, total_inr: int) -> SettledPurchase | None:
    """A shop that settled the checkout itself returns ``{"ok": true,
    "status": "paid", "settled_via": <str>}`` and no payment link. Returns a
    ``SettledPurchase`` only when the payload explicitly says so; ``None``
    otherwise (the normal pay-a-link path)."""
    payload = _payload_from_result(result)
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    settled_via = payload.get("settled_via")
    if not isinstance(settled_via, str) or not settled_via:
        return None
    if payload.get("status") != "paid":
        return None
    amount_inr = payload.get("amount")
    if not isinstance(amount_inr, int) or isinstance(amount_inr, bool):
        amount_inr = None
    return SettledPurchase(total_inr=total_inr, settled_via=settled_via, amount_inr=amount_inr)


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


async def purchase(session: Any, validated: ValidationResult) -> PendingPurchase | SettledPurchase:
    if not validated.ok:
        raise PurchaseError(f"cannot purchase: validation failed ({validated.reason})")

    # One shared cart_id threaded across every item. The first add_to_cart
    # that returns one adopts it; every later call passes it back so all
    # items land in the same cart (Mercator now supports multi-item carts,
    # CLAUDE.md section 3/4).
    cart_id: str | None = None
    for item in validated.items:
        add_args: dict[str, Any] = {
            "product_id": item.product["id"],
            "quantity": item.quantity,
        }
        if cart_id is not None:
            add_args["cart_id"] = cart_id

        add_result = await session.call_tool("add_to_cart", add_args)
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

        returned_cart_id = _cart_id_from_result(add_result)
        if returned_cart_id is None:
            # Shops with no cart_id concept (e.g. tests/stub_shop/server.py,
            # one global cart) -- preserve the original single
            # checkout({"idempotency_key": ...}) shape by leaving cart_id None.
            continue
        if cart_id is None:
            cart_id = returned_cart_id
        elif returned_cart_id != cart_id:
            # We passed a cart_id and the shop made a different cart anyway.
            # The whole-pick-list-in-one-checkout assumption no longer holds;
            # fail loudly (rule #7) rather than checking out a partial cart.
            raise PurchaseError(
                "cannot purchase: shop ignored the passed cart_id "
                f"(sent {cart_id}, got {returned_cart_id} back for item "
                f"{item.product['id']}) -- this pipeline needs every item in one cart"
            )

    idempotency_key = str(uuid.uuid4())
    checkout_args: dict[str, Any] = {"idempotency_key": idempotency_key}
    if cart_id is not None:
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

    settled = _settled_from_result(checkout_result, validated.total_inr)
    if settled is not None:
        return settled

    link = _payment_link_from_result(checkout_result)
    if link is None:
        raise PurchaseError(
            "checkout succeeded but shop returned no payment link "
            f"(idempotency_key={idempotency_key})"
        )

    payment_link_id, payment_link_url, expire_hours = link
    return PendingPurchase(
        payment_link_id=payment_link_id,
        payment_link_url=payment_link_url,
        total_inr=validated.total_inr,
        expire_hours=expire_hours,
    )
