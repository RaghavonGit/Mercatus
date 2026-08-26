import mcp.types as types
import pytest

from emptor.purchase import PurchaseError, purchase
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


def _ok_result(structured_content=None):
    return types.CallToolResult(content=[], structured_content=structured_content, is_error=False)


async def test_purchase_adds_each_item_then_checks_out_once():
    validated = validate_picks(
        [{"product_id": "a", "quantity": 2}, {"product_id": "b", "quantity": 1}],
        CATALOG,
        budget_inr=1000,
    )
    assert validated.ok

    session = _FakeSession(
        {"add_to_cart": _ok_result(), "checkout": _ok_result({"order_id": "order-123"})}
    )

    order_id = await purchase(session, validated)

    assert order_id == "order-123"
    add_calls = [c for c in session.calls if c[0] == "add_to_cart"]
    checkout_calls = [c for c in session.calls if c[0] == "checkout"]
    assert len(add_calls) == 2
    assert len(checkout_calls) == 1


async def test_purchase_uses_fresh_idempotency_key_each_run():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)

    session_1 = _FakeSession(
        {"add_to_cart": _ok_result(), "checkout": _ok_result({"order_id": "order-1"})}
    )
    await purchase(session_1, validated)
    key_1 = session_1.calls[-1][1]["idempotency_key"]

    session_2 = _FakeSession(
        {"add_to_cart": _ok_result(), "checkout": _ok_result({"order_id": "order-2"})}
    )
    await purchase(session_2, validated)
    key_2 = session_2.calls[-1][1]["idempotency_key"]

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
        {"add_to_cart": _ok_result(), "checkout": types.CallToolResult(content=[], is_error=True)}
    )

    with pytest.raises(PurchaseError):
        await purchase(session, validated)


async def test_purchase_checkout_rejection_includes_shop_reason():
    # PUR-07: the shop's rejection reason must be reported verbatim-but-
    # bounded, not swallowed into a generic "checkout failed". Reproduces
    # what a real MCP server actually returns on a tool error (confirmed
    # live against tests/stub_shop): an error CallToolResult with the reason
    # as a text content block, not structured_content.
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
    session = _FakeSession({"add_to_cart": _ok_result(), "checkout": checkout_result})

    with pytest.raises(PurchaseError) as exc_info:
        await purchase(session, validated)

    assert "simulated fraud hold" in str(exc_info.value)


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


async def test_purchase_raises_if_checkout_returns_no_order_id():
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession({"add_to_cart": _ok_result(), "checkout": _ok_result(None)})

    with pytest.raises(PurchaseError):
        await purchase(session, validated)


async def test_purchase_no_order_id_error_includes_idempotency_key():
    # PUR-08: once checkout has "succeeded" but returned no order_id, money
    # may already have moved. The error must carry the idempotency_key so a
    # human can manually reconcile which checkout attempt this was.
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    session = _FakeSession({"add_to_cart": _ok_result(), "checkout": _ok_result(None)})

    with pytest.raises(PurchaseError) as exc_info:
        await purchase(session, validated)

    checkout_call = next(c for c in session.calls if c[0] == "checkout")
    sent_key = checkout_call[1]["idempotency_key"]
    assert sent_key in str(exc_info.value)


class _PerCallSession:
    """Like _FakeSession, but returns a distinct response per call to a given
    tool name, in call order. Needed for PUR-09: _FakeSession returns the same
    canned response for every add_to_cart call, so it can't express "item 2 of
    3 fails" - every call would fail (or succeed) identically."""

    def __init__(self, responses_by_name):
        self._queues = {name: list(results) for name, results in responses_by_name.items()}
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return self._queues[name].pop(0)


async def test_purchase_stops_before_checkout_if_add_to_cart_fails_partway():
    validated = validate_picks(
        [
            {"product_id": "a", "quantity": 1},
            {"product_id": "b", "quantity": 1},
        ],
        CATALOG,
        budget_inr=1000,
    )
    assert validated.ok
    assert len(validated.items) == 2

    session = _PerCallSession(
        {
            "add_to_cart": [
                _ok_result(),
                types.CallToolResult(content=[], is_error=True),
            ],
            "checkout": [_ok_result({"order_id": "should-not-happen"})],
        }
    )

    with pytest.raises(PurchaseError):
        await purchase(session, validated)

    checkout_calls = [c for c in session.calls if c[0] == "checkout"]
    assert checkout_calls == []


async def test_purchase_reads_order_id_from_text_content_when_no_structured_content():
    # Reproduces a real MCP server observed live: a checkout tool that returns
    # a plain dict gets serialized as a JSON text block, not
    # structured_content, unless the tool explicitly opts into a typed output
    # schema. purchase() must still find the order_id.
    validated = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    checkout_result = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"order_id": "order-from-text"}')],
        structured_content=None,
        is_error=False,
    )
    session = _FakeSession({"add_to_cart": _ok_result(), "checkout": checkout_result})

    order_id = await purchase(session, validated)

    assert order_id == "order-from-text"
