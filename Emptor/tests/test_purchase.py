import mcp.types as types
import pytest

from emptor.purchase import PendingPurchase, PurchaseError, SettledPurchase, purchase
from emptor.validate import validate_picks

CATALOG = [
    {"id": "a", "name": "Widget", "price_inr": 100, "in_stock": True},
    {"id": "b", "name": "Gadget", "price_inr": 200, "in_stock": True},
]


class _FakeSession:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self._responses[name]


class _PerCallSession:
    """Returns a distinct response per call to a given tool name, in call
    order -- needed to express "item 2 of 3 behaves differently"."""

    def __init__(self, responses_by_name):
        self._queues = {name: list(results) for name, results in responses_by_name.items()}
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self._queues[name].pop(0)


def _ok_result(structured_content=None):
    return types.CallToolResult(content=[], structured_content=structured_content, is_error=False)


def _add_ok(cart_id="cart-1"):
    return _ok_result({"ok": True, "cart_id": cart_id, "line_total": 100})


def _checkout_ok(cart_id_unused=None):
    return _ok_result(
        {
            "ok": True,
            "payment_link_id": "plink-1",
            "payment_link_url": "https://rzp.io/i/plink-1",
            "status": "pending",
            "expire_hours": 6,
        }
    )


async def test_purchase_adds_each_item_then_checks_out_once():
    validated = validate_picks(
        [{"product_id": "a", "quantity": 2}, {"product_id": "b", "quantity": 1}],
        CATALOG,
        budget_inr=1000,
    )
    assert validated.ok

    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": _checkout_ok()})

    pending = await purchase(session, validated)

    assert isinstance(pending, PendingPurchase)
    assert pending.payment_link_id == "plink-1"
    assert pending.payment_link_url == "https://rzp.io/i/plink-1"
    assert pending.total_inr == validated.total_inr
    assert pending.expire_hours == 6

    add_calls = [c for c in session.calls if c[0] == "add_to_cart"]
    checkout_calls = [c for c in session.calls if c[0] == "checkout"]
    assert len(add_calls) == 2
    assert len(checkout_calls) == 1


async def test_purchase_threads_one_cart_id_across_every_item():
    validated = validate_picks(
        [
            {"product_id": "a", "quantity": 1},
            {"product_id": "b", "quantity": 1},
        ],
        CATALOG,
        budget_inr=1000,
    )
    session = _FakeSession({"add_to_cart": _add_ok("cart-XYZ"), "checkout": _checkout_ok()})

    await purchase(session, validated)

    add_calls = [args for name, args in session.calls if name == "add_to_cart"]
    assert "cart_id" not in add_calls[0]  # first call adopts the cart
    assert add_calls[1]["cart_id"] == "cart-XYZ"  # later calls pass it back
    checkout_args = next(args for name, args in session.calls if name == "checkout")
    assert checkout_args["cart_id"] == "cart-XYZ"


async def test_purchase_raises_if_shop_ignores_passed_cart_id():
    validated = validate_picks(
        [
            {"product_id": "a", "quantity": 1},
            {"product_id": "b", "quantity": 1},
        ],
        CATALOG,
        budget_inr=1000,
    )
    session = _PerCallSession(
        {
            "add_to_cart": [_add_ok("cart-1"), _add_ok("cart-2")],  # shop made a new cart
            "checkout": [_checkout_ok()],
        }
    )

    with pytest.raises(PurchaseError) as exc_info:
        await purchase(session, validated)
    assert "ignored the passed cart_id" in str(exc_info.value)
    assert [c for c in session.calls if c[0] == "checkout"] == []


async def test_purchase_no_cart_id_shop_keeps_single_checkout_shape():
    # A shop that returns no cart_id (one global cart) must still work, with
    # checkout called with just an idempotency_key.
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession({"add_to_cart": _ok_result({"ok": True}), "checkout": _checkout_ok()})

    await purchase(session, validated)

    checkout_args = next(args for name, args in session.calls if name == "checkout")
    assert set(checkout_args) == {"idempotency_key"}


async def test_purchase_uses_fresh_idempotency_key_each_run():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)

    session_1 = _FakeSession({"add_to_cart": _add_ok(), "checkout": _checkout_ok()})
    await purchase(session_1, validated)
    key_1 = next(a for n, a in session_1.calls if n == "checkout")["idempotency_key"]

    session_2 = _FakeSession({"add_to_cart": _add_ok(), "checkout": _checkout_ok()})
    await purchase(session_2, validated)
    key_2 = next(a for n, a in session_2.calls if n == "checkout")["idempotency_key"]

    assert key_1 != key_2


async def test_purchase_rejects_invalid_validation_result():
    validated = validate_picks([], CATALOG, budget_inr=1000)
    assert not validated.ok

    with pytest.raises(PurchaseError):
        await purchase(_FakeSession({}), validated)


async def test_purchase_raises_on_add_to_cart_error():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession({"add_to_cart": types.CallToolResult(content=[], is_error=True)})

    with pytest.raises(PurchaseError):
        await purchase(session, validated)


async def test_purchase_raises_on_checkout_error():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession(
        {"add_to_cart": _add_ok(), "checkout": types.CallToolResult(content=[], is_error=True)}
    )

    with pytest.raises(PurchaseError):
        await purchase(session, validated)


async def test_purchase_checkout_rejection_includes_shop_reason():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    checkout_result = types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text="Error executing tool checkout: simulated fraud hold",
            )
        ],
        is_error=True,
    )
    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": checkout_result})

    with pytest.raises(PurchaseError) as exc_info:
        await purchase(session, validated)

    assert "simulated fraud hold" in str(exc_info.value)


async def test_purchase_checkout_business_rejection_includes_reason():
    # {"ok": false, "reason": ...} inside an otherwise-successful MCP call.
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession(
        {
            "add_to_cart": _add_ok(),
            "checkout": _ok_result({"ok": False, "reason": "TOO_MANY_PENDING_PAYMENT_LINKS"}),
        }
    )
    with pytest.raises(PurchaseError) as exc_info:
        await purchase(session, validated)
    assert "TOO_MANY_PENDING_PAYMENT_LINKS" in str(exc_info.value)


async def test_purchase_add_to_cart_rejection_includes_shop_reason():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    add_to_cart_result = types.CallToolResult(
        content=[types.TextContent(type="text", text="product a is out of stock")],
        is_error=True,
    )
    session = _FakeSession({"add_to_cart": add_to_cart_result})

    with pytest.raises(PurchaseError) as exc_info:
        await purchase(session, validated)

    assert "out of stock" in str(exc_info.value)


@pytest.mark.parametrize(
    "checkout_payload",
    [
        None,
        {"ok": True, "payment_link_url": "https://rzp.io/i/x"},  # missing id
        {"ok": True, "payment_link_id": "plink-1"},  # missing url
        {"ok": True, "payment_link_id": "", "payment_link_url": "https://rzp.io/i/x"},
    ],
)
async def test_purchase_raises_if_checkout_returns_no_payment_link(checkout_payload):
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": _ok_result(checkout_payload)})

    with pytest.raises(PurchaseError):
        await purchase(session, validated)


async def test_purchase_no_payment_link_error_includes_idempotency_key():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": _ok_result(None)})

    with pytest.raises(PurchaseError) as exc_info:
        await purchase(session, validated)

    sent_key = next(a for n, a in session.calls if n == "checkout")["idempotency_key"]
    assert sent_key in str(exc_info.value)


async def test_purchase_stops_before_checkout_if_add_to_cart_fails_partway():
    validated = validate_picks(
        [
            {"product_id": "a", "quantity": 1},
            {"product_id": "b", "quantity": 1},
        ],
        CATALOG,
        budget_inr=1000,
    )
    assert validated.ok and len(validated.items) == 2

    session = _PerCallSession(
        {
            "add_to_cart": [_add_ok(), types.CallToolResult(content=[], is_error=True)],
            "checkout": [_checkout_ok()],
        }
    )

    with pytest.raises(PurchaseError):
        await purchase(session, validated)

    assert [c for c in session.calls if c[0] == "checkout"] == []


async def test_purchase_reads_payment_link_from_text_content_when_no_structured_content():
    # A checkout tool that returns a plain dict gets serialized as a JSON
    # text block, not structured_content, unless it opts into a typed output
    # schema. purchase() must still find the link.
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    checkout_result = types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text='{"ok": true, "payment_link_id": "plink-txt", '
                '"payment_link_url": "https://rzp.io/i/txt", "expire_hours": 6}',
            )
        ],
        structured_content=None,
        is_error=False,
    )
    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": checkout_result})

    pending = await purchase(session, validated)

    assert pending.payment_link_id == "plink-txt"
    assert pending.payment_link_url == "https://rzp.io/i/txt"


def _checkout_autopay_settled():
    return _ok_result(
        {"ok": True, "status": "paid", "amount": 100, "settled_via": "autopay"}
    )


async def test_purchase_returns_settled_purchase_when_shop_autopay_settles():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": _checkout_autopay_settled()})

    result = await purchase(session, validated)

    assert isinstance(result, SettledPurchase)
    assert result.settled_via == "autopay"
    assert result.total_inr == validated.total_inr
    assert result.amount_inr == 100


async def test_purchase_settled_from_text_content_when_no_structured_content():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    checkout_result = types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text='{"ok": true, "status": "paid", "amount": 100, "settled_via": "autopay"}',
            )
        ],
        structured_content=None,
        is_error=False,
    )
    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": checkout_result})

    result = await purchase(session, validated)
    assert isinstance(result, SettledPurchase)
    assert result.settled_via == "autopay"


async def test_purchase_normal_pending_is_not_mistaken_for_settled():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": _checkout_ok()})

    result = await purchase(session, validated)
    assert isinstance(result, PendingPurchase)


async def test_purchase_autopay_fallback_still_returns_a_pending_link():
    # Autopay declined -> shop fell back to a real link; that fallback cause
    # is informational and must not stop Emptor from handling the link.
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    checkout_result = _ok_result(
        {
            "ok": True,
            "payment_link_id": "plink-fb",
            "payment_link_url": "https://rzp.io/i/fb",
            "status": "pending",
            "expire_hours": 6,
            "autopay_fallback_cause": "AUTOPAY_OVER_THRESHOLD",
        }
    )
    session = _FakeSession({"add_to_cart": _add_ok(), "checkout": checkout_result})

    result = await purchase(session, validated)
    assert isinstance(result, PendingPurchase)
    assert result.payment_link_id == "plink-fb"
