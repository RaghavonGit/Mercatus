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
uv run emptor "a birthday gift under 1500 INR" --budget 1500
```

After validating its LLM's picks, Emptor prints the goal, each pick, and
the grand total, then waits for you to type the literal word `yes` to
confirm. This is a second, independent checkpoint on top of the human who
still has to pay the payment link — not a money-safety control (that stays
the shop's job). Pass `--yes` to skip *only* this local prompt (for
scripted/test runs); it does not bypass the shop's guardrails or the
payment step.

Then prints one of:
- `PENDING: pay INR <total> at <payment_link_url> (link id <id>, expires in ~<N>h)`
  — the checkout went through, but no money has moved yet: a human opens
  that link and pays it. The shop's reconciler records the real final
  outcome afterwards (Emptor has already exited). Exit code 0.
- `DECLINED: purchase not confirmed by operator` (stderr) — you didn't type
  `yes`. Not a failure; exit code 0.
- `BLOCKED: <reason>` (stderr, non-zero exit) — Emptor never guesses or
  silently retries a purchase on ambiguity.
- `SUCCESS: …` is retained for a hypothetical future synchronous-payment
  shop; the Payment Link flow above never prints it.

## Shop server contract

Emptor expects the MCP shop server to expose three tools:

| Tool | Arguments | Returns |
|---|---|---|
| `list_products` | none | catalog: a list of products, or `{"products": [...]}`. Each product needs `id, name, price_inr, in_stock`; `description`/`category` optional. |
| `add_to_cart` | `{"product_id": str, "quantity": int, "cart_id"?: str}` | success/error; a `cart_id` in the response is threaded into every later `add_to_cart` and into `checkout` so all items land in one cart. A shop with no `cart_id` concept still works (single global cart). |
| `checkout` | `{"idempotency_key": str, "cart_id"?: str}` | `{"payment_link_id": str, "payment_link_url": str, "status": "pending", "expire_hours"?: int}` on success. A business rejection is `{"ok": false, "reason": …}` inside an otherwise-successful call. |

A shop that returns a *different* `cart_id` after Emptor passed one in is
treated as having ignored it — Emptor raises rather than checking out a
partial cart.

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

Emptor has been run live against the real [Mercator](../Mercator/) package —
real MCP connection, real NIM decision, real [Fides](../Fides/) ledger. That
live run predates the Payment Link migration (it exercised the old
order-creation path); a fresh end-to-end run against the Payment Link flow
is pending. Start Mercator with `MCP_TRANSPORT=streamable-http` in its
`.env` (see its README), then:

```bash
# PowerShell
$env:MERCATOR_ENDPOINT = "http://127.0.0.1:8000/mcp"
uv run emptor "two fantasy novels to read" --budget 1500
```

Multi-item picks now work: Emptor threads one `cart_id` across every
`add_to_cart` call and checks the whole cart out as a single Payment Link.

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

## Known limitations

- **A pending purchase does not survive Emptor exiting.** Emptor prints the
  `PENDING` line and exits; from that point the payment link's fate (paid,
  expired) is tracked only by the shop's own reconciler, in its own
  process. There is no Emptor-side resume — re-running Emptor starts a
  brand-new checkout with a fresh idempotency key.
- The operator-approval prompt reads from stdin. In an environment with no
  usable stdin, use `--yes` (and accept that you've removed the local
  checkpoint).

## Security posture

Emptor assumes adversarial catalog input (see `CLAUDE.md` section 6) and
never handles raw payment credentials. Money-safety enforcement is the
shop's responsibility, not Emptor's — Emptor's own budget check is a sanity
check on its own LLM's output, not a security boundary. Run `uv run
pip-audit` before every push.
