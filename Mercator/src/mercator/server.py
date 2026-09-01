"""MCP server: registers the three Mercator tools and wires guardrails,
idempotency, catalog, cart, payments, and the ledger together.

Zero LLM calls (CLAUDE.md section 2 rule 1). Every tool handler here is a
thin wrapper over the pure/deterministic modules — no business logic lives
in this file. Rejections are ``ok: false`` structured results, not MCP
protocol errors (section 3.1), so any MCP client can read and report the
reason.

Return types are typed models (TypedDict), not bare dicts, so
``structured_content`` populates for every client — confirmed empirically
against this project's installed ``mcp==2.0.0`` (section 8, section 15
2026-08-25 entries).
"""

import os
import threading
from pathlib import Path
from typing import TypedDict

from dotenv import dotenv_values
from mcp.server.mcpserver import MCPServer

from mercator import guardrails, payments
from mercator.cart import CartStore
from mercator.cart import add_to_cart as _add_to_cart
from mercator.catalog import load_catalog
from mercator.config import Config, load_config
from mercator.idempotency import IdempotencyStore, is_valid_idempotency_key, run_with_idempotency
from mercator.ledger import Ledger
from mercator.reasons import MISSING_IDEMPOTENCY_KEY

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = REPO_ROOT / "tests" / "fixtures" / "catalog.json"
DEFAULT_LEDGER_PATH = REPO_ROOT / "ledger.db"


class Product(TypedDict, total=False):
    # total=False: the Python dicts this package actually builds omit
    # absent optional fields entirely (never a literal key with a `None`
    # value). But the SDK fills every declared schema property the client
    # doesn't see supplied with `None` when building structuredContent —
    # so each optional field's type must itself include `None`
    # (`X | None`), or that auto-filled `None` fails the client's own
    # output-schema validation on the real stdio protocol path. See
    # CLAUDE.md section 15's 2026-08-25 "manual Inspector test" entry.
    id: str
    name: str
    price_inr: int
    in_stock: bool
    category: str | None
    description: str | None
    stock: int | None


class ListProductsResult(TypedDict):
    products: list[Product]


class AddToCartResult(TypedDict, total=False):
    ok: bool
    cart_id: str | None
    line_total: int | None
    reason: str | None


class CheckoutResult(TypedDict, total=False):
    ok: bool
    order_id: str | None
    amount: int | None
    status: str | None
    reason: str | None
    detail: str | None


def _safe_log(ledger: Ledger, event_type: str, data: dict) -> None:
    try:
        ledger.log(event_type, data)
    except Exception:
        pass


def _safe_log_checkout_result(ledger: Ledger, cart_id: str, idempotency_key: str, outcome: dict) -> None:
    # Money may have already moved by this point (a Razorpay order was
    # created). A broken ledger callback must never propagate past this
    # call — that would abort `do_checkout` before `run_with_idempotency`
    # stores the result, breaking the mandatory-idempotency guarantee for
    # an already-completed purchase (CLAUDE.md section 2 rules 4 and 6).
    try:
        ledger.log_checkout_result(cart_id, idempotency_key, outcome)
    except Exception:
        pass


def _with_live_stock(cart: dict | None, catalog_by_id: dict[str, dict]) -> dict | None:
    """Refresh a cart's items with CURRENT catalog stock/in_stock before the
    stock guardrail runs, instead of trusting the snapshot ``add_to_cart``
    froze into the cart. Without this, checkout's stock check just re-reads
    its own stale copy and can never actually catch an item that's since
    sold out (the overselling bug found in audit). Price/category are left
    as the frozen snapshot -- only availability needs to be current."""
    if cart is None:
        return None
    live_items = []
    for item in cart.get("items", []):
        live_item = dict(item)
        product = catalog_by_id.get(item.get("product_id"))
        if product is not None:
            stock = product.get("stock")
            live_item["stock"] = stock if isinstance(stock, int) and not isinstance(stock, bool) else 0
            live_item["in_stock"] = bool(product.get("in_stock"))
        else:
            live_item["stock"] = 0
            live_item["in_stock"] = False
        live_items.append(live_item)
    return {**cart, "items": live_items}


def _adjust_stock(cart: dict, catalog_by_id: dict[str, dict], delta_sign: int) -> None:
    """Reserve (delta_sign=-1) or release (delta_sign=+1) each item's
    quantity against the live catalog. Reservation happens only once
    guardrails have already passed, so a guardrail failure (spend cap,
    allowlist) never touches stock at all -- there's nothing to release on
    that path. Release exists for the one path that DOES need it: a
    reservation was committed, but the Razorpay call itself then failed."""
    for item in cart.get("items", []):
        quantity = item.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            continue
        product = catalog_by_id.get(item.get("product_id"))
        if product is not None:
            product["stock"] = product.get("stock", 0) + delta_sign * quantity


def build_server(
    catalog: list[dict],
    config: Config,
    razorpay_client,
    cart_store: CartStore | None = None,
    idempotency_store: IdempotencyStore | None = None,
    ledger: Ledger | None = None,
) -> MCPServer:
    cart_store = cart_store if cart_store is not None else CartStore()
    idempotency_store = idempotency_store if idempotency_store is not None else IdempotencyStore()
    ledger = ledger if ledger is not None else Ledger(DEFAULT_LEDGER_PATH)

    guard_config = {
        "spend_cap_inr": config.spend_cap_inr,
        "allowed_categories": config.allowed_categories,
    }

    catalog_by_id = {p["id"]: p for p in catalog if isinstance(p.get("id"), str)}
    # Guards the "check live stock, then commit/release a reservation"
    # critical section below. Distinct from CartStore's own internal lock:
    # that one protects same-cart double-checkout, this one protects two
    # DIFFERENT carts racing over the same product's stock -- two different
    # cart_ids never collide in CartStore's per-cart bookkeeping, so that
    # lock alone can't prevent overselling across carts. The Razorpay call
    # itself deliberately happens outside this lock so one slow payment
    # doesn't serialize every other checkout in flight.
    stock_lock = threading.Lock()

    for product in catalog:
        if product.get("_flagged"):
            _safe_log(
                ledger,
                "catalog_flagged",
                {"product_id": product.get("id"), "name": product.get("name")},
            )

    server = MCPServer("mercator")

    @server.tool()
    def list_products() -> ListProductsResult:
        return {
            "products": [
                {k: v for k, v in product.items() if not k.startswith("_")}
                for product in catalog
            ]
        }

    @server.tool()
    def add_to_cart(product_id: str, quantity: int) -> AddToCartResult:
        return _add_to_cart(catalog, cart_store, product_id, quantity)

    @server.tool()
    def checkout(cart_id: str, idempotency_key: str) -> CheckoutResult:
        if not is_valid_idempotency_key(idempotency_key):
            outcome = {"ok": False, "reason": MISSING_IDEMPOTENCY_KEY}
            _safe_log_checkout_result(ledger, cart_id, idempotency_key, outcome)
            return outcome

        def do_checkout() -> dict:
            cart = cart_store.begin_checkout(cart_id)
            with stock_lock:
                live_cart = _with_live_stock(cart, catalog_by_id)
                guard_result = guardrails.run_all_guardrails(
                    live_cart, idempotency_key, guard_config, log_fn=ledger.log_guardrail_check
                )
                if guard_result.passed:
                    # Commit the reservation now, inside the same locked
                    # section that just read live stock -- otherwise two
                    # concurrent checkouts for different carts could both
                    # observe the last unit as available before either
                    # decrements it.
                    _adjust_stock(cart, catalog_by_id, -1)

            if not guard_result.passed:
                if cart is not None:
                    cart_store.abort_checkout(cart_id)
                outcome = {"ok": False, "reason": guard_result.reason, "detail": guard_result.detail}
                _safe_log_checkout_result(ledger, cart_id, idempotency_key, outcome)
                return outcome

            total = sum(item["price_inr"] * item["quantity"] for item in cart["items"])
            outcome = payments.create_order(razorpay_client, total, receipt=cart_id)
            if outcome.get("ok"):
                cart_store.complete_checkout(cart_id)
            else:
                # Payment failed after the reservation was committed -- release
                # it so the item isn't permanently phantom-sold, and let the
                # cart be retried under a different idempotency key.
                with stock_lock:
                    _adjust_stock(cart, catalog_by_id, 1)
                cart_store.abort_checkout(cart_id)
            _safe_log_checkout_result(ledger, cart_id, idempotency_key, outcome)
            return outcome

        return run_with_idempotency(idempotency_store, idempotency_key, do_checkout)

    return server


def load_env() -> dict:
    # Absolute path, not the CWD-relative ".env" — an MCP client spawns
    # this server with an arbitrary working directory, so a relative
    # lookup would fail to find the file under the exact launch path
    # CLAUDE.md section 14 requires this to work under.
    return {**dotenv_values(REPO_ROOT / ".env"), **os.environ}


def main() -> None:
    env = load_env()
    config = load_config(env)
    razorpay_client = payments.make_client(
        config.razorpay_key_id, config.razorpay_key_secret, config.razorpay_mode
    )
    catalog = load_catalog(DEFAULT_CATALOG_PATH)

    server = build_server(
        catalog=catalog,
        config=config,
        razorpay_client=razorpay_client,
        ledger=Ledger(DEFAULT_LEDGER_PATH),
    )

    # Default stays stdio (Claude Desktop, MCP Inspector, section 15's
    # verified real-client testing all target this). Emptor's client
    # (emptor/client.py) speaks streamable_http_client against
    # MERCATOR_ENDPOINT instead, so opt into that transport here with
    # MCP_TRANSPORT=streamable-http when running against Emptor — see
    # CLAUDE.md section 15, 2026-08-26 entry for the gap this closes.
    if env.get("MCP_TRANSPORT") == "streamable-http":
        server.run(transport="streamable-http", host="127.0.0.1", port=config.port)
    else:
        server.run()


if __name__ == "__main__":
    main()
