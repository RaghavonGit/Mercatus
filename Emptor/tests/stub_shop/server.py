r"""Minimal MCP shop server for exercising Emptor's Steps 1/2/6 against a real
MCP connection.

Not a real shop (no payments, no auth) and not Mercator - a throwaway double
that implements just enough of the shop contract (list_products, add_to_cart,
checkout) to run Emptor's full pipeline end to end. By default the catalog is
tests/fixtures/mock_catalog.json, the same fixture the unit tests use - but
that file is asserted against by the unit tests, so don't edit it to try your
own products. Point STUB_CATALOG_PATH at your own JSON file instead (see the
schema in README.md's "Shop server contract" table: id, name, price_inr,
in_stock required; description, category optional).

This file is deliberately NOT under tests/test_*.py - pytest's testpaths
config only collects test_*.py, so it is never auto-run as a test. It is
meant to be started as a standalone process:

    uv run python tests/stub_shop/server.py

    # or with your own catalog:
    (PowerShell)  $env:STUB_CATALOG_PATH = "C:\path\to\your_catalog.json"
                  uv run python tests/stub_shop/server.py

Then, in another terminal, point Emptor's real CLI at it:

    (PowerShell)  $env:MERCATOR_ENDPOINT = "http://127.0.0.1:8000/mcp"
                  uv run emptor "a birthday gift for a student who loves stationery" --budget 500

That is a full live run: real MCP connection -> real catalog discovery ->
real local-LLM decision -> real validation -> real (stub) checkout.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from mcp.server.mcpserver import MCPServer

_DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "mock_catalog.json"
CATALOG_PATH = Path(os.environ.get("STUB_CATALOG_PATH") or _DEFAULT_CATALOG_PATH)

mcp = MCPServer("stub-shop")

_catalog: list[dict] = json.loads(CATALOG_PATH.read_text())
_catalog_by_id = {item["id"]: item for item in _catalog}
_cart: list[dict] = []
_links: dict[str, dict] = {}


@mcp.tool()
def list_products() -> dict:
    """Return the full catalog."""
    return {"products": _catalog}


@mcp.tool()
def add_to_cart(product_id: str, quantity: int) -> dict:
    """Add a product to the in-memory cart."""
    product = _catalog_by_id.get(product_id)
    if product is None:
        raise ValueError(f"unknown product_id: {product_id!r}")
    if not product["in_stock"]:
        raise ValueError(f"product {product_id!r} is out of stock")
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    _cart.append({"product_id": product_id, "quantity": quantity})
    return {"ok": True}


@mcp.tool()
def checkout(idempotency_key: str) -> dict:
    """Finalize the cart into a (fake) payment link, idempotently on
    idempotency_key. Mirrors Mercator's post-migration contract: no money
    moves here, the caller gets a pending link back."""
    if idempotency_key in _links:
        return _links[idempotency_key]
    if not _cart:
        raise ValueError("cannot checkout an empty cart")
    link_id = f"stub-plink-{uuid.uuid4()}"
    result = {
        "ok": True,
        "payment_link_id": link_id,
        "payment_link_url": f"https://stub.example/pay/{link_id}",
        "status": "pending",
        "expire_hours": 6,
    }
    _links[idempotency_key] = result
    _cart.clear()
    return result


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)
