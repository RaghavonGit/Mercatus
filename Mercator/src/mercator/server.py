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
import sys
import threading
import time
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
from mercator.reasons import MISSING_IDEMPOTENCY_KEY, TOO_MANY_PENDING_PAYMENT_LINKS
from mercator.spend_tracker import SpendTracker

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = REPO_ROOT / "tests" / "fixtures" / "catalog.json"
DEFAULT_LEDGER_PATH = REPO_ROOT / "ledger.db"
DEFAULT_SPEND_TRACKER_PATH = REPO_ROOT / "spend_tracker.db"

# Fallback used only when PAYMENT_LINK_EXPIRE_HOURS is unset (test mode
# only -- config.py makes it required in live mode). Kept short so an
# abandoned link and its stock reservation don't linger.
DEFAULT_PAYMENT_LINK_EXPIRE_HOURS = 6

# How often the in-process reconciler polls Razorpay for the status of
# every payment link still awaiting payment. Design plan: 30-60s is plenty
# for this volume and stays well under Razorpay's undocumented rate limit.
POLL_INTERVAL_SECONDS = 45


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
    # Breaking change from v1's order_id/status: a successful checkout now
    # returns a *payment link* the buyer pays themselves. No money has
    # moved when this is returned -- status is "pending" until the
    # in-process reconciler confirms payment.
    ok: bool
    payment_link_id: str | None
    payment_link_url: str | None
    amount: int | None
    status: str | None
    expire_hours: int | None
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


def _cancel_orphaned_payment_links(razorpay_client, ledger: Ledger) -> None:
    """Startup cleanup: every payment link left "created"/"issued" from a
    previous process is orphaned -- this process has no record of which
    cart it belonged to, so it can never be reconciled. Cancel them all so
    a buyer can't pay a link nothing is watching.

    NOTE: this cancels every open link on the Razorpay account, not just
    ones this deployment created (documented in CLAUDE.md). Each cancel is
    isolated so one failure doesn't stop the rest; a listing failure at
    boot (no network) is logged to stderr and startup continues."""
    try:
        listing = razorpay_client.payment_link.all()
    except Exception as exc:  # noqa: BLE001 -- fail open, never block startup
        print(
            f"mercator: could not list payment links for startup cleanup ({exc}); continuing",
            file=sys.stderr,
        )
        return

    for entry in payments._iter_payment_links(listing):
        if not isinstance(entry, dict) or entry.get("status") not in ("created", "issued"):
            continue
        link_id = entry.get("id")
        if not link_id:
            continue
        try:
            razorpay_client.payment_link.cancel(link_id)
            _safe_log(ledger, "startup_link_cancelled", {"payment_link_id": link_id})
        except Exception as exc:  # noqa: BLE001 -- one bad link must not stop cleanup
            print(f"mercator: could not cancel orphaned link {link_id} ({exc})", file=sys.stderr)


def build_server(
    catalog: list[dict],
    config: Config,
    razorpay_client,
    cart_store: CartStore | None = None,
    idempotency_store: IdempotencyStore | None = None,
    ledger: Ledger | None = None,
    spend_tracker: SpendTracker | None = None,
) -> MCPServer:
    cart_store = cart_store if cart_store is not None else CartStore()
    idempotency_store = idempotency_store if idempotency_store is not None else IdempotencyStore()
    ledger = ledger if ledger is not None else Ledger(DEFAULT_LEDGER_PATH)
    spend_tracker = (
        spend_tracker if spend_tracker is not None else SpendTracker(DEFAULT_SPEND_TRACKER_PATH)
    )

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

    # cart_id -> {payment_link_id, cart, total, idempotency_key, partial_logged}.
    # Reconciliation bookkeeping only -- deliberately separate from
    # CartStore (cart/checkout state) and deliberately in-memory: a restart
    # cancels every open link (see _cancel_orphaned_payment_links), so
    # there is nothing here worth persisting. Its own lock. Lock ordering
    # in this file is always stock_lock -> pending_lock; the reconciler
    # never holds pending_lock while taking stock_lock.
    pending_links: dict[str, dict] = {}
    pending_lock = threading.Lock()

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
    def add_to_cart(product_id: str, quantity: int, cart_id: str | None = None) -> AddToCartResult:
        return _add_to_cart(catalog, cart_store, product_id, quantity, cart_id)

    @server.tool()
    def checkout(cart_id: str, idempotency_key: str) -> CheckoutResult:
        if not is_valid_idempotency_key(idempotency_key):
            outcome = {"ok": False, "reason": MISSING_IDEMPOTENCY_KEY}
            _safe_log_checkout_result(ledger, cart_id, idempotency_key, outcome)
            return outcome

        def do_checkout() -> dict:
            cart = cart_store.begin_checkout(cart_id)
            total = 0

            # One critical section: read live stock -> run guardrails ->
            # check max-pending -> check cumulative cap -> commit the
            # reservation. Splitting the lock between any of these would let
            # two concurrent checkouts both clear a check the other's
            # in-flight reservation should have failed. The plan's ordered
            # steps are logical precedence, not lock boundaries.
            with stock_lock:
                live_cart = _with_live_stock(cart, catalog_by_id)
                guard_result = guardrails.run_all_guardrails(
                    live_cart, idempotency_key, guard_config, log_fn=ledger.log_guardrail_check
                )

                rejection: dict | None = None
                if not guard_result.passed:
                    rejection = {
                        "ok": False,
                        "reason": guard_result.reason,
                        "detail": guard_result.detail,
                    }
                else:
                    total = sum(item["price_inr"] * item["quantity"] for item in cart["items"])

                    with pending_lock:
                        pending_count = len(pending_links)
                    if pending_count >= config.max_pending_payment_links:
                        rejection = {
                            "ok": False,
                            "reason": TOO_MANY_PENDING_PAYMENT_LINKS,
                            "detail": (
                                f"{pending_count} payment links already awaiting payment "
                                f"(max {config.max_pending_payment_links})"
                            ),
                        }
                    elif config.cumulative_spend_cap_inr is not None:
                        already_spent = spend_tracker.sum_since(config.cumulative_spend_window_hours)
                        cumulative = guardrails.check_cumulative_spend_cap(
                            already_spent, total, config.cumulative_spend_cap_inr
                        )
                        if not cumulative.passed:
                            rejection = {
                                "ok": False,
                                "reason": cumulative.reason,
                                "detail": cumulative.detail,
                            }

                if rejection is None:
                    _adjust_stock(cart, catalog_by_id, -1)

            if rejection is not None:
                if cart is not None:
                    cart_store.abort_checkout(cart_id)
                _safe_log_checkout_result(ledger, cart_id, idempotency_key, rejection)
                return rejection

            # All checks passed, stock is reserved. The Razorpay call
            # itself happens outside stock_lock so one slow payment doesn't
            # serialize every other checkout.
            expire_hours = config.payment_link_expire_hours or DEFAULT_PAYMENT_LINK_EXPIRE_HOURS
            outcome = payments.create_payment_link(razorpay_client, total, cart_id, expire_hours)
            if outcome.get("ok"):
                # Surface the window so a client (Emptor) can tell the buyer
                # how long they have -- it has no other way to know it.
                outcome["expire_hours"] = expire_hours
                with pending_lock:
                    pending_links[cart_id] = {
                        "payment_link_id": outcome["payment_link_id"],
                        "cart": cart,
                        "total": total,
                        "idempotency_key": idempotency_key,
                        "partial_logged": False,
                    }
                # A pending link is still a one-shot terminal resolution of
                # this cart_id -- same meaning complete_checkout already has
                # everywhere else. The reconciler logs the real final outcome.
                cart_store.complete_checkout(cart_id)
            else:
                # Link creation failed after the reservation was committed --
                # release it so the item isn't phantom-sold, and let the cart
                # be retried under a different idempotency key.
                with stock_lock:
                    _adjust_stock(cart, catalog_by_id, 1)
                cart_store.abort_checkout(cart_id)
            _safe_log_checkout_result(ledger, cart_id, idempotency_key, outcome)
            return outcome

        return run_with_idempotency(idempotency_store, idempotency_key, do_checkout)

    def reconcile_once() -> None:
        """Poll every payment link still awaiting payment and resolve the
        ones that reached a terminal state. Callable directly from a test
        with no thread. Snapshot under pending_lock, then do all network
        calls and stock/ledger work outside it so a slow poll never blocks
        a new checkout from registering."""
        with pending_lock:
            snapshot = list(pending_links.items())

        for cid, entry in snapshot:
            result = payments.fetch_payment_link(razorpay_client, entry["payment_link_id"])
            status = result.get("status")

            if status == "paid":
                with pending_lock:
                    removed = pending_links.pop(cid, None)
                if removed is None:
                    continue
                spend_tracker.record_paid(removed["total"])
                _safe_log_checkout_result(
                    ledger,
                    cid,
                    removed["idempotency_key"],
                    {
                        "ok": True,
                        "payment_link_id": removed["payment_link_id"],
                        "amount": removed["total"],
                        "status": "paid",
                    },
                )

            elif status in ("expired", "cancelled"):
                with pending_lock:
                    removed = pending_links.pop(cid, None)
                if removed is None:
                    continue
                with stock_lock:
                    _adjust_stock(removed["cart"], catalog_by_id, 1)
                _safe_log_checkout_result(
                    ledger,
                    cid,
                    removed["idempotency_key"],
                    {
                        "ok": False,
                        "payment_link_id": removed["payment_link_id"],
                        "amount": removed["total"],
                        "status": status,
                        "reason": "PAYMENT_FAILED",
                    },
                )

            elif status == "partially_paid":
                # Rare with accept_partial=False. Log exactly once, keep the
                # entry pending -- never fold it silently into paid/expired.
                with pending_lock:
                    current = pending_links.get(cid)
                    if current is None or current["partial_logged"]:
                        continue
                    current["partial_logged"] = True
                    to_log = dict(current)
                _safe_log_checkout_result(
                    ledger,
                    cid,
                    to_log["idempotency_key"],
                    {
                        "ok": False,
                        "payment_link_id": to_log["payment_link_id"],
                        "amount": to_log["total"],
                        "status": "partially_paid",
                    },
                )
            # "pending" / "unknown" -> no action; "unknown" fails closed
            # toward "keep waiting".

    # main() and the tests both reach the reconciler and the pending-links
    # view through these handles, so the background thread and the checkout
    # tool operate on the exact same objects.
    server._reconcile_once = reconcile_once
    server._pending_links = pending_links
    server._spend_tracker = spend_tracker
    server._catalog_by_id = catalog_by_id

    return server


def load_env() -> dict:
    # Absolute path, not the CWD-relative ".env" — an MCP client spawns
    # this server with an arbitrary working directory, so a relative
    # lookup would fail to find the file under the exact launch path
    # CLAUDE.md section 14 requires this to work under.
    return {**dotenv_values(REPO_ROOT / ".env"), **os.environ}


def _reconcile_loop(reconcile_once) -> None:
    """Body of the background daemon thread. MCPServer.run() is fully
    synchronous and blocks for the whole process lifetime with no event
    loop to attach to, so a plain thread is the only shape that works. It
    must never die: any exception from one poll is swallowed so the next
    poll still happens."""
    while True:
        try:
            reconcile_once()
        except Exception as exc:  # noqa: BLE001 -- a dead reconciler is worse
            print(f"mercator: reconcile pass failed ({exc}); will retry", file=sys.stderr)
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    env = load_env()
    config = load_config(env)
    razorpay_client = payments.make_client(
        config.razorpay_key_id, config.razorpay_key_secret, config.razorpay_mode
    )
    catalog = load_catalog(DEFAULT_CATALOG_PATH)

    # One Ledger for the whole process: fides.Ledger holds its own
    # connection + lock, and the hash-chain has no cross-connection mutual
    # exclusion, so a second instance on the same file could interleave
    # appends. Startup cleanup, the checkout tool, and the reconciler all
    # share this one.
    ledger = Ledger(DEFAULT_LEDGER_PATH)

    server = build_server(
        catalog=catalog,
        config=config,
        razorpay_client=razorpay_client,
        ledger=ledger,
        spend_tracker=SpendTracker(DEFAULT_SPEND_TRACKER_PATH),
    )

    # Orphaned links from a previous run can never be reconciled by this
    # process -- cancel them before anyone can pay one.
    _cancel_orphaned_payment_links(razorpay_client, ledger)

    reconciler = threading.Thread(
        target=_reconcile_loop, args=(server._reconcile_once,), daemon=True, name="mercator-reconcile"
    )
    reconciler.start()

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
