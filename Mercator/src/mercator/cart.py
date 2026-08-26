"""Cart state management.

Builds cart items from the server-side catalog only — a client only ever
supplies a ``product_id``/``quantity`` pair, never a price or stock count
(CLAUDE.md section 4's anti-tampering rule). The cart dict this module
produces is exactly the shape ``guardrails.run_all_guardrails`` expects.

v1 scope: quantity must be exactly 1, and each ``add_to_cart`` call creates
a fresh single-item cart (no multi-item carts yet — matches Emptor's
current scope, CLAUDE.md section 17).
"""

import threading
import uuid
from collections import OrderedDict

from mercator.limits import MAX_CARTS


class CartStore:
    """Thread-safe on its own: the MCP SDK dispatches concurrent tool calls
    to separate threads (confirmed empirically against this project's
    installed ``mcp`` SDK), so two ``checkout`` calls for the same cart_id
    can race for real, not just in program order. Every method below holds
    the same internal lock, so ``begin_checkout``'s claim-if-available check
    is atomic regardless of what else is calling into this store."""

    def __init__(self, max_carts: int = MAX_CARTS) -> None:
        self._carts: OrderedDict[str, dict] = OrderedDict()
        self._checked_out: set[str] = set()
        self._in_progress: set[str] = set()
        self._max_carts = max_carts
        self._lock = threading.Lock()

    def create(self, items: list[dict]) -> str:
        cart_id = f"cart_{uuid.uuid4().hex}"
        with self._lock:
            self._carts[cart_id] = {"cart_id": cart_id, "items": items}
            self._evict_if_needed()
        return cart_id

    def _evict_if_needed(self) -> None:
        # Caller must already hold self._lock.
        while len(self._carts) > self._max_carts:
            for candidate_id in self._carts:
                if candidate_id not in self._checked_out and candidate_id not in self._in_progress:
                    del self._carts[candidate_id]
                    break
            else:
                # Every remaining cart is checked-out or mid-checkout --
                # nothing safe to evict, so stop rather than evict one of
                # those (CLAUDE.md section 2 rule 4: fail closed, never
                # discard state a client might still rely on).
                break

    def get(self, cart_id: str) -> dict | None:
        with self._lock:
            if cart_id in self._checked_out:
                return None
            return self._carts.get(cart_id)

    def begin_checkout(self, cart_id: str) -> dict | None:
        """Atomically claim a cart for checkout: returns it (and marks it
        in-progress) only if it exists and isn't already checked out or
        already mid-checkout. Must be paired with exactly one of
        ``complete_checkout``/``abort_checkout``."""
        with self._lock:
            if cart_id in self._checked_out or cart_id in self._in_progress:
                return None
            cart = self._carts.get(cart_id)
            if cart is None:
                return None
            self._in_progress.add(cart_id)
            return cart

    def complete_checkout(self, cart_id: str) -> None:
        with self._lock:
            self._checked_out.add(cart_id)
            self._in_progress.discard(cart_id)

    def abort_checkout(self, cart_id: str) -> None:
        with self._lock:
            self._in_progress.discard(cart_id)

    def mark_checked_out(self, cart_id: str) -> None:
        with self._lock:
            self._checked_out.add(cart_id)
            self._in_progress.discard(cart_id)


def add_to_cart(catalog: list[dict], store: CartStore, product_id, quantity) -> dict:
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity != 1:
        return {"ok": False, "reason": "INVALID_QUANTITY"}

    product = next((p for p in catalog if p.get("id") == product_id), None)
    if product is None:
        return {"ok": False, "reason": "PRODUCT_NOT_FOUND"}

    stock = product.get("stock")
    # A missing/malformed stock count is undeterminable availability, not
    # "assume enough" — fail closed to 0 rather than defaulting to a value
    # that trivially always passes (CLAUDE.md section 2 rule 4).
    available = stock if isinstance(stock, int) and not isinstance(stock, bool) else 0
    if not product.get("in_stock") or quantity > available:
        return {"ok": False, "reason": "OUT_OF_STOCK"}

    price_inr = product["price_inr"]
    item = {
        "product_id": product["id"],
        "quantity": quantity,
        "price_inr": price_inr,
        "category": product.get("category"),
        "in_stock": product["in_stock"],
        "stock": available,
    }
    cart_id = store.create([item])
    return {"ok": True, "cart_id": cart_id, "line_total": price_inr * quantity}
