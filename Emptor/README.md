# Emptor

The autonomous shopper agent of the Mercatus project. Given a goal in plain
English and a budget, Emptor connects to any MCP-compatible shop server,
reads its catalog, decides what to buy (via one LLM call), and requests a
purchase. It never decides whether a purchase is *allowed* — that authority
belongs entirely to the shop server.

Part of the [Mercatus](../README.md) project — see the root README for how
Emptor, Mercator, and Fides fit together.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Configure

```bash
cp .env.example .env
# then edit .env:
#   NIM_API_KEY=<your NVIDIA NIM key from build.nvidia.com>
#   MERCATOR_ENDPOINT=<your MCP shop server's URL>
#   DEFAULT_BUDGET_INR=<fallback budget if --budget isn't passed>
```

## Run

```bash
uv run emptor "a birthday gift under ₹1500" --budget 1500
```

Prints either:
- `SUCCESS: bought <items> for ₹<total> (order <id>)`, or
- `BLOCKED: <reason>` (to stderr, non-zero exit code) — Emptor never guesses
  or silently retries a purchase on ambiguity.

## Shop server contract

Emptor expects the MCP shop server to expose three tools:

| Tool | Arguments | Returns |
|---|---|---|
| `list_products` | none | catalog: a list of products, or `{"products": [...]}`. Each product needs `id, name, price_inr, in_stock`; `description`/`category` optional. |
| `add_to_cart` | `{"product_id": str, "quantity": int}` | success/error |
| `checkout` | `{"idempotency_key": str}` | `{"order_id": str}` on success |

Emptor also enforces a sanity cap on its own LLM's picks before purchasing —
not a money-safety control (that's the shop's job), just a bound against a
runaway or hallucinated response: at most **10 distinct products**, and at
most **quantity 10 per item**. A pick list outside either bound is rejected
outright, with no partial purchase.

## Testing

```bash
uv run pytest -v
```

The suite above runs entirely against mocks — no real LLM, no real shop
needed. To exercise the real pipeline end to end (real MCP connection, real
NIM decision, real checkout), a minimal stub shop server is included at
`tests/stub_shop/server.py`. It is not Mercator and has no payments — it just
implements the three-tool contract above, backed by
`tests/fixtures/mock_catalog.json`, so Steps 1/2/6 can be proven against a
real MCP connection before Mercator exists.

### Running against the real Mercator

Emptor has also been run live against the real [Mercator](../Mercator/)
package — real MCP connection, real NIM decision, real Razorpay test-mode
order, real [Fides](../Fides/) ledger. Start Mercator with
`MCP_TRANSPORT=streamable-http` in its `.env` (see its README), then:

```bash
# PowerShell
$env:MERCATOR_ENDPOINT = "http://127.0.0.1:8000/mcp"
uv run emptor "a fantasy novel to read" --budget 500
```

**Known gap**: pick a goal that resolves to exactly **one** product.
Mercator's cart is single-item-only (each `add_to_cart` opens its own fresh
cart), and this pipeline has no way to check out more than one such cart in
a single order yet — a multi-item pick against Mercator fails loudly with a
clear `PurchaseError` rather than silently doing the wrong thing. This is an
open design question, not a bug; see `CLAUDE.md` section 10.

```bash
# terminal 1
uv run python tests/stub_shop/server.py

# terminal 2 (PowerShell)
$env:MERCATOR_ENDPOINT = "http://127.0.0.1:8000/mcp"
uv run emptor "a birthday gift for a student who loves stationery" --budget 500
```

Requires a real `NIM_API_KEY` in `.env` — this makes a real NIM API call.

To try your own products instead of the default fixture, point
`STUB_CATALOG_PATH` at your own JSON file (same shape as the table above —
see `tests/stub_shop/sample_catalog.json` for an example). Don't edit
`tests/fixtures/mock_catalog.json` itself — the unit tests assert against its
exact contents.

```bash
# terminal 1 (PowerShell)
$env:STUB_CATALOG_PATH = "tests\stub_shop\sample_catalog.json"
uv run python tests/stub_shop/server.py

# terminal 2 (PowerShell)
$env:MERCATOR_ENDPOINT = "http://127.0.0.1:8000/mcp"
uv run emptor "a comfortable typing setup" --budget 5000
```

## Security posture

Emptor assumes adversarial catalog input (see `CLAUDE.md` section 6) and
never handles raw payment credentials. Money-safety enforcement is the
shop's responsibility, not Emptor's — Emptor's own budget check is a sanity
check on its own LLM's output, not a security boundary. Run `uv run
pip-audit` before every push.
