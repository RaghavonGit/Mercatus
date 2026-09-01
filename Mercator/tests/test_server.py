import copy
from unittest.mock import Mock

import jsonschema
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.func_metadata import func_metadata

import mercator.server as server_module
from mercator.cart import CartStore
from mercator.config import Config
from mercator.idempotency import IdempotencyStore
from mercator.ledger import Ledger
from mercator.server import (
    AddToCartResult,
    CheckoutResult,
    Product,
    _cancel_orphaned_payment_links,
    build_server,
    load_env,
)
from mercator.spend_tracker import SpendTracker

CATALOG = [
    {"id": "prod_001", "name": "The Hobbit", "price_inr": 450, "in_stock": True, "category": "books", "stock": 12, "description": "A novel.", "_flagged": False},
    {"id": "prod_over_cap", "name": "Expensive Thing", "price_inr": 9999, "in_stock": True, "category": "books", "stock": 5, "description": "Pricey.", "_flagged": False},
]

TEST_CONFIG = Config(
    razorpay_key_id="rzp_test_abc",
    razorpay_key_secret="secret",
    spend_cap_inr=1500,
    allowed_categories=["books", "toys", "stationery"],
    port=8000,
    razorpay_mode="test",
    cumulative_spend_cap_inr=None,
    cumulative_spend_window_hours=24,
    payment_link_expire_hours=None,
    max_pending_payment_links=5,
)


def make_fake_razorpay_client(
    link_response=None,
    create_raises=None,
    all_response=None,
    all_raises=None,
    fetch_response=None,
):
    """Every payment_link method is stubbed explicitly -- a bare Mock()
    silently fails closed (create returns a Mock -> recovery -> .all()
    returns a Mock -> [] -> PAYMENT_FAILED), which hides which path a test
    is actually exercising."""
    client = Mock()
    if create_raises is not None:
        client.payment_link.create.side_effect = create_raises
    else:
        client.payment_link.create.return_value = link_response or {
            "id": "plink_1",
            "short_url": "https://rzp.io/i/plink_1",
            "status": "created",
        }
    if all_raises is not None:
        client.payment_link.all.side_effect = all_raises
    else:
        client.payment_link.all.return_value = all_response or {"payment_links": []}
    client.payment_link.fetch.return_value = fetch_response or {
        "status": "created",
        "amount": 0,
        "amount_paid": 0,
    }
    client.payment_link.cancel.return_value = {"status": "cancelled"}
    return client


def make_server(
    tmp_path,
    catalog=None,
    razorpay_client=None,
    config=None,
    ledger=None,
    spend_tracker=None,
):
    # deepcopy, not list(): checkout mutates a product dict's "stock" in
    # place (the reservation fix), so a shallow copy would still let one
    # test's committed reservation leak into every other test sharing the
    # module-level CATALOG constant.
    return build_server(
        catalog=catalog if catalog is not None else copy.deepcopy(CATALOG),
        config=config or TEST_CONFIG,
        razorpay_client=razorpay_client or make_fake_razorpay_client(),
        cart_store=CartStore(),
        idempotency_store=IdempotencyStore(),
        ledger=ledger if ledger is not None else Ledger(tmp_path / "ledger.db"),
        spend_tracker=spend_tracker
        if spend_tracker is not None
        else SpendTracker(tmp_path / "spend.db"),
    )


CONFIG_WITH_CUMULATIVE_CAP = Config(
    razorpay_key_id="rzp_test_abc",
    razorpay_key_secret="secret",
    spend_cap_inr=100_000,  # per-transaction cap out of the way for these tests
    allowed_categories=["books", "toys", "stationery"],
    port=8000,
    razorpay_mode="test",
    cumulative_spend_cap_inr=1000,
    cumulative_spend_window_hours=24,
    payment_link_expire_hours=None,
    max_pending_payment_links=5,
)


def paid_fetch(amount_inr):
    paise = amount_inr * 100
    return {"status": "paid", "amount": paise, "amount_paid": paise}


def server_stock(server, product_id):
    return server._catalog_by_id[product_id]["stock"]


# --- output schema vs. actual runtime response shape ----------------------
#
# The MCP SDK auto-fills any TypedDict key absent from a tool's returned
# dict with `None` when building structuredContent, to match the full set
# of declared output-schema properties. A real MCP client (the reference
# ClientSession, and the MCP Inspector) validates that structuredContent
# against the tool's declared outputSchema and raises/rejects if it
# doesn't conform — this is a real step in the actual stdio protocol path
# that `server.call_tool()` (used by every other test in this file) does
# NOT exercise, which is why these dedicated tests exist.


def _output_schema(typed_dict_cls):
    def _fn() -> typed_dict_cls: ...

    return func_metadata(_fn).output_schema


def _as_sdk_would_fill(schema: dict, partial: dict) -> dict:
    filled = dict(partial)
    for key in schema.get("properties", {}):
        filled.setdefault(key, None)
    return filled


def test_add_to_cart_success_shape_validates_against_declared_schema():
    schema = _output_schema(AddToCartResult)
    filled = _as_sdk_would_fill(schema, {"ok": True, "cart_id": "cart_1", "line_total": 450})
    jsonschema.validate(filled, schema)


def test_add_to_cart_failure_shape_validates_against_declared_schema():
    schema = _output_schema(AddToCartResult)
    filled = _as_sdk_would_fill(schema, {"ok": False, "reason": "PRODUCT_NOT_FOUND"})
    jsonschema.validate(filled, schema)


def test_checkout_success_shape_validates_against_declared_schema():
    schema = _output_schema(CheckoutResult)
    filled = _as_sdk_would_fill(
        schema,
        {
            "ok": True,
            "payment_link_id": "plink_1",
            "payment_link_url": "https://rzp.io/i/plink_1",
            "amount": 450,
            "status": "pending",
            "expire_hours": 6,
        },
    )
    jsonschema.validate(filled, schema)


def test_checkout_failure_shape_validates_against_declared_schema():
    schema = _output_schema(CheckoutResult)
    filled = _as_sdk_would_fill(
        schema, {"ok": False, "reason": "SPEND_CAP_EXCEEDED", "detail": "Cart total 2000 exceeds cap 1500"}
    )
    jsonschema.validate(filled, schema)


def test_product_with_missing_optional_fields_validates_against_declared_schema():
    schema = _output_schema(Product)
    filled = _as_sdk_would_fill(
        schema, {"id": "prod_x", "name": "X", "price_inr": 1, "in_stock": True}
    )
    jsonschema.validate(filled, schema)


# --- tool discovery -------------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_tools_are_discoverable(tmp_path):
    server = make_server(tmp_path)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {"list_products", "add_to_cart", "checkout"}


# --- list_products ---------------------------------------------------------


@pytest.mark.asyncio
async def test_list_products_returns_structured_content(tmp_path):
    server = make_server(tmp_path)
    result = await server.call_tool("list_products", {})
    assert result.structured_content is not None
    products = result.structured_content["products"]
    ids = {p["id"] for p in products}
    assert ids == {"prod_001", "prod_over_cap"}


@pytest.mark.asyncio
async def test_list_products_does_not_leak_flagged_field(tmp_path):
    server = make_server(tmp_path)
    result = await server.call_tool("list_products", {})
    for product in result.structured_content["products"]:
        assert "_flagged" not in product


# --- add_to_cart -------------------------------------------------------


@pytest.mark.asyncio
async def test_add_to_cart_valid_product(tmp_path):
    server = make_server(tmp_path)
    result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    assert result.structured_content["ok"] is True
    assert result.structured_content["line_total"] == 450


@pytest.mark.asyncio
async def test_add_to_cart_unknown_product(tmp_path):
    server = make_server(tmp_path)
    result = await server.call_tool("add_to_cart", {"product_id": "does_not_exist", "quantity": 1})
    assert result.structured_content["ok"] is False
    assert result.structured_content["reason"] == "PRODUCT_NOT_FOUND"


# --- checkout ------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_full_success_flow(tmp_path):
    client = make_fake_razorpay_client(
        link_response={"id": "plink_1", "short_url": "https://rzp.io/i/plink_1", "status": "created"}
    )
    server = make_server(tmp_path, razorpay_client=client)
    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]

    checkout_result = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    body = checkout_result.structured_content
    assert body["ok"] is True
    assert body["payment_link_id"] == "plink_1"
    assert body["payment_link_url"] == "https://rzp.io/i/plink_1"
    assert body["amount"] == 450
    assert body["status"] == "pending"
    assert body["expire_hours"] == 6  # DEFAULT_PAYMENT_LINK_EXPIRE_HOURS (config has None)

    # a pending-links entry was registered against this cart
    assert cart_id in server._pending_links
    assert server._pending_links[cart_id]["total"] == 450


@pytest.mark.asyncio
async def test_checkout_refused_when_max_pending_links_reached(tmp_path):
    config = Config(
        razorpay_key_id="rzp_test_abc",
        razorpay_key_secret="secret",
        spend_cap_inr=100_000,
        allowed_categories=["books", "toys", "stationery"],
        port=8000,
        razorpay_mode="test",
        cumulative_spend_cap_inr=None,
        cumulative_spend_window_hours=24,
        payment_link_expire_hours=None,
        max_pending_payment_links=1,
    )
    server = make_server(tmp_path, config=config)

    first_add = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    second_add = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})

    ok = await server.call_tool(
        "checkout", {"cart_id": first_add.structured_content["cart_id"], "idempotency_key": "idem-1"}
    )
    assert ok.structured_content["ok"] is True

    stock_before = server_stock(server, "prod_001")
    refused = await server.call_tool(
        "checkout", {"cart_id": second_add.structured_content["cart_id"], "idempotency_key": "idem-2"}
    )
    assert refused.structured_content["ok"] is False
    assert refused.structured_content["reason"] == "TOO_MANY_PENDING_PAYMENT_LINKS"
    # refusal is before the reservation -- no stock touched
    assert server_stock(server, "prod_001") == stock_before


@pytest.mark.asyncio
async def test_checkout_refused_when_cumulative_cap_would_be_exceeded(tmp_path):
    server = make_server(tmp_path, config=CONFIG_WITH_CUMULATIVE_CAP)
    server._spend_tracker.record_paid(800)  # 800 already spent, cap is 1000

    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]  # 450 -> 800 + 450 = 1250 > 1000

    stock_before = server_stock(server, "prod_001")
    result = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    assert result.structured_content["ok"] is False
    assert result.structured_content["reason"] == "CUMULATIVE_SPEND_CAP_EXCEEDED"
    assert server_stock(server, "prod_001") == stock_before


@pytest.mark.asyncio
async def test_checkout_cumulative_cap_is_a_noop_when_unconfigured(tmp_path):
    # TEST_CONFIG has cumulative_spend_cap_inr=None: a huge prior spend
    # must not matter.
    server = make_server(tmp_path)
    server._spend_tracker.record_paid(9_999_999)

    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]
    result = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    assert result.structured_content["ok"] is True


@pytest.mark.asyncio
async def test_checkout_missing_idempotency_key(tmp_path):
    server = make_server(tmp_path)
    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]

    result = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": ""})
    assert result.structured_content["ok"] is False
    assert result.structured_content["reason"] == "MISSING_IDEMPOTENCY_KEY"


@pytest.mark.asyncio
async def test_checkout_cart_not_found(tmp_path):
    server = make_server(tmp_path)
    result = await server.call_tool("checkout", {"cart_id": "cart_does_not_exist", "idempotency_key": "idem-1"})
    assert result.structured_content["ok"] is False
    assert result.structured_content["reason"] == "CART_NOT_FOUND"


@pytest.mark.asyncio
async def test_checkout_spend_cap_exceeded(tmp_path):
    server = make_server(tmp_path)
    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_over_cap", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]

    result = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    assert result.structured_content["ok"] is False
    assert result.structured_content["reason"] == "SPEND_CAP_EXCEEDED"


@pytest.mark.asyncio
async def test_checkout_same_idempotency_key_does_not_charge_twice(tmp_path):
    client = make_fake_razorpay_client()
    server = make_server(tmp_path, razorpay_client=client)
    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]

    first = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    second = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})

    assert first.structured_content == second.structured_content
    assert client.payment_link.create.call_count == 1


@pytest.mark.asyncio
async def test_checkout_payment_failure_returns_payment_failed(tmp_path):
    client = make_fake_razorpay_client(
        create_raises=RuntimeError("boom"), all_response={"payment_links": []}
    )
    server = make_server(tmp_path, razorpay_client=client)
    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]

    result = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    assert result.structured_content["ok"] is False
    assert result.structured_content["reason"] == "PAYMENT_FAILED"
    assert cart_id not in server._pending_links


# --- load_env: must not depend on the process CWD ------------------------


def test_load_env_finds_dotenv_regardless_of_cwd(tmp_path, monkeypatch):
    fake_repo_root = tmp_path / "fake_repo"
    fake_repo_root.mkdir()
    (fake_repo_root / ".env").write_text("MERCATOR_TEST_MARKER=found_it\n", encoding="utf-8")

    other_dir = tmp_path / "somewhere_else"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    monkeypatch.setattr(server_module, "REPO_ROOT", fake_repo_root)
    monkeypatch.delenv("MERCATOR_TEST_MARKER", raising=False)

    env = load_env()
    assert env.get("MERCATOR_TEST_MARKER") == "found_it"


# --- malformed input / unknown tool -----------------------------------


@pytest.mark.asyncio
async def test_malformed_add_to_cart_input_raises_tool_error_not_crash(tmp_path):
    server = make_server(tmp_path)
    with pytest.raises(ToolError):
        await server.call_tool("add_to_cart", {"product_id": "prod_001"})  # missing quantity


@pytest.mark.asyncio
async def test_unknown_tool_name_raises_clean_error(tmp_path):
    server = make_server(tmp_path)
    with pytest.raises(ToolError):
        await server.call_tool("delete_everything", {})


# --- flagged catalog entries reach the ledger for review -----------------


@pytest.mark.asyncio
async def test_flagged_catalog_products_are_logged_for_review(tmp_path):
    flagged_catalog = [
        {"id": "prod_clean", "name": "Clean Product", "price_inr": 10, "in_stock": True, "category": "books", "stock": 5, "_flagged": False},
        {"id": "prod_hostile", "name": "Widget [REDACTED]", "price_inr": 10, "in_stock": True, "category": "books", "stock": 5, "_flagged": True},
    ]
    spy_ledger = Mock()
    build_server(
        catalog=flagged_catalog,
        config=TEST_CONFIG,
        razorpay_client=make_fake_razorpay_client(),
        cart_store=CartStore(),
        idempotency_store=IdempotencyStore(),
        ledger=spy_ledger,
        spend_tracker=SpendTracker(tmp_path / "spend.db"),
    )

    spy_ledger.log.assert_called_once_with(
        "catalog_flagged", {"product_id": "prod_hostile", "name": "Widget [REDACTED]"}
    )


# --- ledger integration --------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_survives_throwing_ledger_and_still_stores_idempotency_result(tmp_path):
    client = make_fake_razorpay_client()
    broken_ledger = Mock()
    broken_ledger.log_guardrail_check = Mock()
    broken_ledger.log_checkout_result = Mock(side_effect=RuntimeError("ledger unreachable"))

    server = build_server(
        catalog=copy.deepcopy(CATALOG),
        config=TEST_CONFIG,
        razorpay_client=client,
        cart_store=CartStore(),
        idempotency_store=IdempotencyStore(),
        ledger=broken_ledger,
        spend_tracker=SpendTracker(tmp_path / "spend.db"),
    )
    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]

    result = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    assert result.structured_content["ok"] is True
    assert result.structured_content["payment_link_id"] == "plink_1"

    # A broken ledger must not defeat the mandatory idempotency guarantee:
    # a retry with the same key must still replay, not re-charge.
    second = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    assert second.structured_content["payment_link_id"] == "plink_1"
    assert client.payment_link.create.call_count == 1


# --- audit fix: stock is actually re-checked and reserved at checkout ----

def make_low_stock_catalog():
    # A fresh list of fresh dicts every call -- do_checkout now mutates a
    # product's "stock" in place (that's the reservation fix), so reusing
    # one shared module-level list/dicts across tests (list(SHARED_CONST)
    # only copies the outer list, not the inner dicts) would let one test's
    # committed reservation leak into the next test's starting stock.
    return [
        {
            "id": "prod_last_unit",
            "name": "Rare Item",
            "price_inr": 100,
            "in_stock": True,
            "category": "books",
            "stock": 1,
            "description": "Only one left.",
            "_flagged": False,
        },
    ]


@pytest.mark.asyncio
async def test_checkout_prevents_overselling_the_last_unit_across_two_carts(tmp_path):
    client = make_fake_razorpay_client()
    server = make_server(tmp_path, catalog=make_low_stock_catalog(), razorpay_client=client)

    first_add = await server.call_tool("add_to_cart", {"product_id": "prod_last_unit", "quantity": 1})
    second_add = await server.call_tool("add_to_cart", {"product_id": "prod_last_unit", "quantity": 1})
    first_cart_id = first_add.structured_content["cart_id"]
    second_cart_id = second_add.structured_content["cart_id"]

    first_checkout = await server.call_tool("checkout", {"cart_id": first_cart_id, "idempotency_key": "idem-1"})
    second_checkout = await server.call_tool("checkout", {"cart_id": second_cart_id, "idempotency_key": "idem-2"})

    assert first_checkout.structured_content["ok"] is True
    assert second_checkout.structured_content["ok"] is False
    assert second_checkout.structured_content["reason"] == "OUT_OF_STOCK"
    assert client.payment_link.create.call_count == 1


@pytest.mark.asyncio
async def test_checkout_payment_failure_releases_the_stock_reservation(tmp_path):
    failing_client = make_fake_razorpay_client(
        create_raises=RuntimeError("boom"), all_response={"payment_links": []}
    )
    server = make_server(tmp_path, catalog=make_low_stock_catalog(), razorpay_client=failing_client)

    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_last_unit", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]
    failed_checkout = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    assert failed_checkout.structured_content["reason"] == "PAYMENT_FAILED"

    # Stock must be restored -- a fresh cart for the still-only unit must
    # still be purchasable, proving the failed checkout didn't permanently
    # consume inventory it never actually paid for.
    retry_add = await server.call_tool("add_to_cart", {"product_id": "prod_last_unit", "quantity": 1})
    assert retry_add.structured_content["ok"] is True


@pytest.mark.asyncio
async def test_checkout_rechecks_live_catalog_stock_not_the_stale_cart_snapshot(tmp_path):
    catalog = [
        {"id": "prod_x", "name": "Widget", "price_inr": 50, "in_stock": True, "category": "books", "stock": 1, "_flagged": False}
    ]
    server = make_server(tmp_path, catalog=catalog)

    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_x", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]

    # Simulate the merchant's live catalog changing between add_to_cart and
    # checkout -- this is the same list object the server holds, exactly
    # what a real restock/correction mechanism would mutate.
    catalog[0]["stock"] = 0
    catalog[0]["in_stock"] = False

    result = await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})
    assert result.structured_content["ok"] is False
    assert result.structured_content["reason"] == "OUT_OF_STOCK"


@pytest.mark.asyncio
async def test_checkout_writes_to_ledger(tmp_path):
    client = make_fake_razorpay_client()
    ledger = Ledger(tmp_path / "ledger.db")
    server = make_server(tmp_path, razorpay_client=client, ledger=ledger)
    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]
    await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": "idem-1"})

    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked >= 6  # six guardrail checks + final checkout_result


# --- reconcile_once ------------------------------------------------------


async def _checkout_one(server, product_id="prod_001", key="idem-1"):
    add_result = await server.call_tool("add_to_cart", {"product_id": product_id, "quantity": 1})
    cart_id = add_result.structured_content["cart_id"]
    await server.call_tool("checkout", {"cart_id": cart_id, "idempotency_key": key})
    return cart_id


@pytest.mark.asyncio
async def test_reconcile_once_paid_records_spend_and_clears_pending(tmp_path):
    client = make_fake_razorpay_client()
    server = make_server(tmp_path, razorpay_client=client)
    cart_id = await _checkout_one(server)
    assert cart_id in server._pending_links

    client.payment_link.fetch.return_value = paid_fetch(450)
    server._reconcile_once()

    assert cart_id not in server._pending_links
    assert server._spend_tracker.sum_since(24) == 450


@pytest.mark.asyncio
async def test_reconcile_once_paid_then_counts_toward_cumulative_cap(tmp_path):
    client = make_fake_razorpay_client()
    server = make_server(tmp_path, razorpay_client=client, config=CONFIG_WITH_CUMULATIVE_CAP)
    await _checkout_one(server, key="idem-1")  # 450 pending
    client.payment_link.fetch.return_value = paid_fetch(450)
    server._reconcile_once()  # 450 now recorded as spent, cap is 1000

    # a second 450 checkout would push cumulative to 900 -- still ok
    add_result = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    second = await server.call_tool(
        "checkout", {"cart_id": add_result.structured_content["cart_id"], "idempotency_key": "idem-2"}
    )
    assert second.structured_content["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["expired", "cancelled"])
async def test_reconcile_once_expired_or_cancelled_releases_stock(tmp_path, terminal):
    client = make_fake_razorpay_client()
    server = make_server(tmp_path, catalog=make_low_stock_catalog(), razorpay_client=client)
    cart_id = await _checkout_one(server, product_id="prod_last_unit")
    assert server_stock(server, "prod_last_unit") == 0

    client.payment_link.fetch.return_value = {"status": terminal, "amount": 10000, "amount_paid": 0}
    server._reconcile_once()

    assert cart_id not in server._pending_links
    assert server_stock(server, "prod_last_unit") == 1
    assert server._spend_tracker.sum_since(24) == 0


@pytest.mark.asyncio
async def test_reconcile_once_partially_paid_logs_exactly_once(tmp_path):
    client = make_fake_razorpay_client()
    spy_ledger = Mock()
    spy_ledger.log_guardrail_check = Mock()
    server = make_server(tmp_path, razorpay_client=client, ledger=spy_ledger)
    cart_id = await _checkout_one(server)

    client.payment_link.fetch.return_value = {
        "status": "partially_paid",
        "amount": 45000,
        "amount_paid": 20000,
    }
    spy_ledger.log_checkout_result.reset_mock()
    server._reconcile_once()
    server._reconcile_once()
    server._reconcile_once()

    partial_logs = [
        c for c in spy_ledger.log_checkout_result.call_args_list if c.args[2].get("status") == "partially_paid"
    ]
    assert len(partial_logs) == 1
    assert cart_id in server._pending_links  # stays pending, never folded away


@pytest.mark.asyncio
async def test_reconcile_once_pending_and_unknown_are_noops(tmp_path):
    client = make_fake_razorpay_client()
    server = make_server(tmp_path, razorpay_client=client)
    cart_id = await _checkout_one(server)

    for status in ("created", "issued", "weird_new_status"):
        client.payment_link.fetch.return_value = {"status": status, "amount": 45000, "amount_paid": 0}
        server._reconcile_once()
        assert cart_id in server._pending_links
    assert server._spend_tracker.sum_since(24) == 0


# --- startup cleanup of orphaned links ----------------------------------


def test_cancel_orphaned_payment_links_cancels_created_and_issued_only(tmp_path):
    client = Mock()
    client.payment_link.all.return_value = {
        "payment_links": [
            {"id": "plink_created", "status": "created"},
            {"id": "plink_issued", "status": "issued"},
            {"id": "plink_paid", "status": "paid"},
            {"id": "plink_expired", "status": "expired"},
        ]
    }
    _cancel_orphaned_payment_links(client, Ledger(tmp_path / "ledger.db"))

    cancelled = {c.args[0] for c in client.payment_link.cancel.call_args_list}
    assert cancelled == {"plink_created", "plink_issued"}


def test_cancel_orphaned_payment_links_one_bad_cancel_does_not_stop_the_rest(tmp_path):
    client = Mock()
    client.payment_link.all.return_value = {
        "payment_links": [
            {"id": "plink_a", "status": "created"},
            {"id": "plink_b", "status": "created"},
        ]
    }
    client.payment_link.cancel.side_effect = [RuntimeError("nope"), {"status": "cancelled"}]
    _cancel_orphaned_payment_links(client, Ledger(tmp_path / "ledger.db"))
    assert client.payment_link.cancel.call_count == 2


def test_cancel_orphaned_payment_links_listing_failure_is_swallowed(tmp_path):
    client = Mock()
    client.payment_link.all.side_effect = RuntimeError("no network at boot")
    _cancel_orphaned_payment_links(client, Ledger(tmp_path / "ledger.db"))  # must not raise
    client.payment_link.cancel.assert_not_called()
