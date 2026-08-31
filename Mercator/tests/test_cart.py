import threading

import pytest

from mercator.cart import CartStore, add_to_cart
from mercator.guardrails import check_input

CATALOG = [
    {"id": "prod_001", "name": "The Hobbit", "price_inr": 450, "in_stock": True, "category": "books", "stock": 12},
    {"id": "prod_002", "name": "Wooden Puzzle", "price_inr": 300, "in_stock": True, "category": "toys", "stock": 8},
    {"id": "prod_005", "name": "Recycled Notebook", "price_inr": 150, "in_stock": False, "category": "stationery", "stock": 0},
    {"id": "prod_low_stock", "name": "Rare Item", "price_inr": 999, "in_stock": True, "category": "toys", "stock": 0},
    {"id": "prod_no_stock_field", "name": "No Stock Field", "price_inr": 100, "in_stock": True, "category": "books"},
]


def test_add_to_cart_valid_product_returns_ok_with_cart_id_and_line_total():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    assert result["ok"] is True
    assert isinstance(result["cart_id"], str) and result["cart_id"]
    assert result["line_total"] == 450


def test_add_to_cart_product_not_found_rejects():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_nonexistent", 1)
    assert result == {"ok": False, "reason": "PRODUCT_NOT_FOUND"}


def test_add_to_cart_out_of_stock_rejects():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_005", 1)
    assert result == {"ok": False, "reason": "OUT_OF_STOCK"}


def test_add_to_cart_quantity_exceeds_stock_rejects():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_low_stock", 1)
    assert result == {"ok": False, "reason": "OUT_OF_STOCK"}


@pytest.mark.parametrize("bad_quantity", [0, -1, "1", True])
def test_add_to_cart_invalid_quantity_rejects(bad_quantity):
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", bad_quantity)
    assert result == {"ok": False, "reason": "INVALID_QUANTITY"}


def test_add_to_cart_two_calls_produce_different_cart_ids():
    store = CartStore()
    first = add_to_cart(CATALOG, store, "prod_001", 1)
    second = add_to_cart(CATALOG, store, "prod_001", 1)
    assert first["cart_id"] != second["cart_id"]


def test_add_to_cart_no_stock_field_rejects_as_out_of_stock():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_no_stock_field", 1)
    assert result == {"ok": False, "reason": "OUT_OF_STOCK"}


def test_cartstore_get_returns_stored_cart():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    cart = store.get(result["cart_id"])
    assert cart is not None
    assert cart["cart_id"] == result["cart_id"]
    assert len(cart["items"]) == 1


def test_cartstore_get_unknown_cart_returns_none():
    store = CartStore()
    assert store.get("cart_does_not_exist") is None


def test_cartstore_get_returns_none_after_checked_out():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    store.mark_checked_out(result["cart_id"])
    assert store.get(result["cart_id"]) is None


def test_add_to_cart_stored_cart_is_valid_guardrails_input():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    cart = store.get(result["cart_id"])
    item = cart["items"][0]
    for field in ("product_id", "quantity", "price_inr", "category", "in_stock", "stock"):
        assert field in item
    assert check_input(cart).passed is True


# --- begin_checkout / complete_checkout / abort_checkout (audit fix: -----
# --- prevents two concurrent checkout calls from both claiming a cart) ---


def test_begin_checkout_unknown_cart_returns_none():
    store = CartStore()
    assert store.begin_checkout("cart_does_not_exist") is None


def test_begin_checkout_returns_the_cart_and_marks_it_in_progress():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    cart = store.begin_checkout(result["cart_id"])
    assert cart is not None
    assert cart["cart_id"] == result["cart_id"]


def test_begin_checkout_second_call_while_first_in_progress_returns_none():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    first = store.begin_checkout(result["cart_id"])
    second = store.begin_checkout(result["cart_id"])
    assert first is not None
    assert second is None


def test_begin_checkout_already_checked_out_returns_none():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    store.begin_checkout(result["cart_id"])
    store.complete_checkout(result["cart_id"])
    assert store.begin_checkout(result["cart_id"]) is None


def test_abort_checkout_allows_a_later_begin_checkout():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    store.begin_checkout(result["cart_id"])
    store.abort_checkout(result["cart_id"])
    assert store.begin_checkout(result["cart_id"]) is not None


def test_complete_checkout_makes_get_return_none():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    store.begin_checkout(result["cart_id"])
    store.complete_checkout(result["cart_id"])
    assert store.get(result["cart_id"]) is None


# --- bounded cart storage (audit fix: unbounded memory growth) -----------


def test_cartstore_evicts_oldest_untouched_cart_past_max_carts():
    store = CartStore(max_carts=2)
    first = add_to_cart(CATALOG, store, "prod_001", 1)["cart_id"]
    add_to_cart(CATALOG, store, "prod_001", 1)["cart_id"]
    add_to_cart(CATALOG, store, "prod_001", 1)["cart_id"]
    assert store.get(first) is None


def test_begin_checkout_is_safe_under_genuine_concurrent_threads():
    # The functional (sequential) tests above prove begin_checkout's logic
    # is correct; this proves that logic actually holds when two OS threads
    # race it for real (not just two calls in program order), which is the
    # scenario the audit found unprotected -- the MCP SDK dispatches
    # concurrent tool calls to separate threads (confirmed empirically
    # against this project's installed mcp SDK before this fix).
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1)
    cart_id = result["cart_id"]

    winners = []
    lock = threading.Lock()
    start_barrier = threading.Barrier(2)

    def attempt():
        start_barrier.wait()
        claimed = store.begin_checkout(cart_id)
        if claimed is not None:
            with lock:
                winners.append(claimed)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1


def test_cartstore_never_evicts_a_cart_mid_checkout():
    store = CartStore(max_carts=1)
    first = add_to_cart(CATALOG, store, "prod_001", 1)["cart_id"]
    store.begin_checkout(first)
    add_to_cart(CATALOG, store, "prod_001", 1)  # would normally evict `first`
    # still present (not evicted) -- get() only returns None once checked out,
    # so a non-None result here proves `first` is still in the store, just
    # correctly locked out of a second concurrent begin_checkout.
    assert store.get(first) is not None


# --- CartStore.add_item (multi-item cart support) -------------------------


def make_item(product_id="prod_001", quantity=1, **overrides):
    item = {
        "product_id": product_id,
        "quantity": quantity,
        "price_inr": 450,
        "category": "books",
        "in_stock": True,
        "stock": 12,
    }
    item.update(overrides)
    return item


def test_add_item_new_product_appends_a_new_line():
    store = CartStore()
    cart_id = store.create([make_item("prod_001", 1)])
    assert store.add_item(cart_id, make_item("prod_005", 2)) is True
    cart = store.get(cart_id)
    assert len(cart["items"]) == 2
    product_ids = [item["product_id"] for item in cart["items"]]
    assert product_ids == ["prod_001", "prod_005"]


def test_add_item_same_product_merges_quantity_into_existing_line():
    store = CartStore()
    cart_id = store.create([make_item("prod_001", 2)])
    assert store.add_item(cart_id, make_item("prod_001", 3)) is True
    cart = store.get(cart_id)
    assert len(cart["items"]) == 1
    assert cart["items"][0]["quantity"] == 5


def test_add_item_same_product_refreshes_snapshot_fields_last_write_wins():
    store = CartStore()
    cart_id = store.create([make_item("prod_001", 1, price_inr=450, category="books", in_stock=True, stock=12)])
    store.add_item(cart_id, make_item("prod_001", 1, price_inr=500, category="toys", in_stock=False, stock=3))
    cart = store.get(cart_id)
    item = cart["items"][0]
    assert item["quantity"] == 2
    assert item["price_inr"] == 500
    assert item["category"] == "toys"
    assert item["in_stock"] is False
    assert item["stock"] == 3


def test_add_item_checked_out_cart_returns_false():
    store = CartStore()
    cart_id = store.create([make_item("prod_001", 1)])
    store.mark_checked_out(cart_id)
    assert store.add_item(cart_id, make_item("prod_005", 1)) is False


def test_add_item_in_progress_cart_returns_false():
    store = CartStore()
    cart_id = store.create([make_item("prod_001", 1)])
    store.begin_checkout(cart_id)
    assert store.add_item(cart_id, make_item("prod_005", 1)) is False


def test_add_item_nonexistent_cart_returns_false():
    store = CartStore()
    assert store.add_item("cart_does_not_exist", make_item("prod_001", 1)) is False


def test_add_item_never_raises_on_bad_cart_id():
    store = CartStore()
    # Same "never raises" contract as every other CartStore method -- a bad
    # cart_id is just another way of "not found", not an exception path.
    assert store.add_item(None, make_item("prod_001", 1)) is False


# --- add_to_cart(cart_id=...) (multi-item cart support) --------------------


def test_add_to_cart_cart_id_none_behaves_identically_to_today():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1, cart_id=None)
    assert result["ok"] is True
    assert isinstance(result["cart_id"], str) and result["cart_id"]
    assert result["line_total"] == 450
    cart = store.get(result["cart_id"])
    assert len(cart["items"]) == 1


def test_add_to_cart_quantity_two_now_succeeds():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 2)
    assert result["ok"] is True
    assert result["line_total"] == 900


def test_add_to_cart_quantity_higher_than_two_now_succeeds():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 5)
    assert result["ok"] is True
    assert result["line_total"] == 2250


def test_add_to_cart_with_existing_cart_id_appends_new_line_for_different_product():
    store = CartStore()
    first = add_to_cart(CATALOG, store, "prod_001", 1)
    second = add_to_cart(CATALOG, store, "prod_002", 1, cart_id=first["cart_id"])
    assert second["ok"] is True
    assert second["cart_id"] == first["cart_id"]
    assert second["line_total"] == 300
    cart = store.get(first["cart_id"])
    assert len(cart["items"]) == 2


def test_add_to_cart_with_existing_cart_id_merges_same_product():
    store = CartStore()
    first = add_to_cart(CATALOG, store, "prod_001", 1)
    second = add_to_cart(CATALOG, store, "prod_001", 2, cart_id=first["cart_id"])
    assert second["ok"] is True
    assert second["line_total"] == 900  # this call's own added line total
    cart = store.get(first["cart_id"])
    assert len(cart["items"]) == 1
    assert cart["items"][0]["quantity"] == 3


def test_add_to_cart_bad_cart_id_returns_cart_not_found():
    store = CartStore()
    result = add_to_cart(CATALOG, store, "prod_001", 1, cart_id="cart_does_not_exist")
    assert result == {"ok": False, "reason": "CART_NOT_FOUND"}


def test_add_to_cart_checked_out_cart_id_returns_cart_not_found():
    store = CartStore()
    first = add_to_cart(CATALOG, store, "prod_001", 1)
    store.mark_checked_out(first["cart_id"])
    result = add_to_cart(CATALOG, store, "prod_005", 1, cart_id=first["cart_id"])
    assert result == {"ok": False, "reason": "CART_NOT_FOUND"}


def test_add_to_cart_existing_cart_id_still_validates_product_not_found():
    store = CartStore()
    first = add_to_cart(CATALOG, store, "prod_001", 1)
    result = add_to_cart(CATALOG, store, "prod_nonexistent", 1, cart_id=first["cart_id"])
    assert result == {"ok": False, "reason": "PRODUCT_NOT_FOUND"}


def test_add_to_cart_existing_cart_id_still_validates_out_of_stock():
    store = CartStore()
    first = add_to_cart(CATALOG, store, "prod_001", 1)
    result = add_to_cart(CATALOG, store, "prod_005", 1, cart_id=first["cart_id"])
    assert result == {"ok": False, "reason": "OUT_OF_STOCK"}
