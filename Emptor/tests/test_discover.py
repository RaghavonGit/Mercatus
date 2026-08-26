import mcp.types as types
import pytest

from emptor.discover import DiscoverError, discover_catalog


class _FakeSession:
    def __init__(self, result):
        self._result = result
        self.called_with = None

    async def call_tool(self, name, arguments=None):
        self.called_with = (name, arguments)
        return self._result


async def test_discover_catalog_uses_structured_content():
    result = types.CallToolResult(
        content=[],
        structured_content=[{"id": "a", "name": "A", "price_inr": 100, "in_stock": True}],
    )
    session = _FakeSession(result)

    catalog = await discover_catalog(session)

    assert catalog == [{"id": "a", "name": "A", "price_inr": 100, "in_stock": True}]
    assert session.called_with == ("list_products", None)


async def test_discover_catalog_falls_back_to_text_content():
    result = types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text='[{"id": "a", "name": "A", "price_inr": 100, "in_stock": true}]',
            )
        ],
        structured_content=None,
    )
    session = _FakeSession(result)

    catalog = await discover_catalog(session)

    assert catalog[0]["id"] == "a"


async def test_discover_catalog_unwraps_products_key():
    result = types.CallToolResult(
        content=[],
        structured_content={"products": [{"id": "a", "name": "A", "price_inr": 100, "in_stock": True}]},
    )
    session = _FakeSession(result)

    catalog = await discover_catalog(session)

    assert catalog == [{"id": "a", "name": "A", "price_inr": 100, "in_stock": True}]


async def test_discover_catalog_raises_on_missing_field():
    result = types.CallToolResult(content=[], structured_content=[{"id": "a", "name": "A"}])
    session = _FakeSession(result)

    with pytest.raises(DiscoverError):
        await discover_catalog(session)


async def test_discover_catalog_raises_when_not_a_list():
    result = types.CallToolResult(content=[], structured_content={"unexpected": "shape"})
    session = _FakeSession(result)

    with pytest.raises(DiscoverError):
        await discover_catalog(session)


async def test_discover_catalog_raises_on_tool_error():
    result = types.CallToolResult(content=[], is_error=True)
    session = _FakeSession(result)

    with pytest.raises(DiscoverError):
        await discover_catalog(session)


async def test_discover_catalog_raises_on_negative_price():
    result = types.CallToolResult(
        content=[],
        structured_content=[{"id": "a", "name": "A", "price_inr": -100, "in_stock": True}],
    )
    session = _FakeSession(result)

    with pytest.raises(DiscoverError):
        await discover_catalog(session)


async def test_discover_catalog_raises_on_non_bool_in_stock():
    result = types.CallToolResult(
        content=[],
        structured_content=[{"id": "a", "name": "A", "price_inr": 100, "in_stock": "yes"}],
    )
    session = _FakeSession(result)

    with pytest.raises(DiscoverError):
        await discover_catalog(session)


async def test_discover_catalog_raises_on_non_string_id():
    result = types.CallToolResult(
        content=[],
        structured_content=[{"id": 123, "name": "A", "price_inr": 100, "in_stock": True}],
    )
    session = _FakeSession(result)

    with pytest.raises(DiscoverError):
        await discover_catalog(session)


async def test_discover_catalog_raises_on_duplicate_id():
    result = types.CallToolResult(
        content=[],
        structured_content=[
            {"id": "a", "name": "A", "price_inr": 100, "in_stock": True},
            {"id": "a", "name": "A-dupe", "price_inr": 50, "in_stock": True},
        ],
    )
    session = _FakeSession(result)

    with pytest.raises(DiscoverError):
        await discover_catalog(session)


async def test_discover_catalog_raises_on_non_string_name():
    result = types.CallToolResult(
        content=[],
        structured_content=[{"id": "a", "name": 123, "price_inr": 100, "in_stock": True}],
    )
    session = _FakeSession(result)

    with pytest.raises(DiscoverError):
        await discover_catalog(session)


async def test_discover_catalog_preserves_optional_fields():
    result = types.CallToolResult(
        content=[],
        structured_content=[
            {
                "id": "a",
                "name": "A",
                "price_inr": 100,
                "in_stock": True,
                "description": "A cozy widget",
                "category": "widgets",
            }
        ],
    )
    session = _FakeSession(result)

    catalog = await discover_catalog(session)

    assert catalog[0]["description"] == "A cozy widget"
    assert catalog[0]["category"] == "widgets"


async def test_discover_catalog_handles_large_catalog():
    big_catalog = [
        {"id": f"sku-{i}", "name": f"Item {i}", "price_inr": 100, "in_stock": True}
        for i in range(10_000)
    ]
    result = types.CallToolResult(content=[], structured_content=big_catalog)
    session = _FakeSession(result)

    catalog = await discover_catalog(session)

    assert len(catalog) == 10_000
    assert catalog[-1]["id"] == "sku-9999"


async def test_discover_catalog_accepts_float_price():
    # Product.price_inr is declared int | float (DIS-15): a shop that
    # serializes a non-integer price (e.g. 899.5) must still be accepted,
    # for shop-agnostic graceful degradation. This is a deliberate ruling,
    # not an oversight — see CLAUDE.md Decision Log, 2026-08-25.
    result = types.CallToolResult(
        content=[],
        structured_content=[{"id": "a", "name": "A", "price_inr": 899.5, "in_stock": True}],
    )
    session = _FakeSession(result)

    catalog = await discover_catalog(session)

    assert catalog[0]["price_inr"] == 899.5
