# Mercator

The merchant agent of the **Mercatus** project — an MCP server that hosts a
product catalog and enforces every rule about whether a purchase is allowed
to complete. Part of the [Mercatus](../README.md) project — see the root
README for how Emptor, Mercator, and Fides fit together.

Zero LLM calls. Every guardrail is deterministic, server-side, fail-closed.

## Status

All core modules are built and tested: guardrails, idempotency, catalog
sanitization, cart, config, payments, ledger, and the MCP server itself
(three tools: `list_products`, `add_to_cart`, `checkout`). 196 tests passing.

- **Payments**: verified against the **real** Razorpay test-mode API (not
  just mocks) — order creation, amount echoing, and idempotency (no
  double-charge on a replayed key) all confirmed live.
- **Real MCP client**: tested over an actual stdio subprocess (the official
  MCP Inspector, and a raw `ClientSession`) — caught and fixed a real
  output-schema bug that in-process testing alone had missed.
- **Security audit**: a full-codebase pass found and fixed 7 issues,
  including a real overselling bug (stock was never re-verified or reserved
  at checkout) and a real concurrency race (two checkouts for the last unit
  in stock could both succeed) — the concurrency fix was verified against a
  real stdio subprocess racing two genuine concurrent checkout calls.
- **End-to-end with the real Emptor agent**: run and verified live — see
  below.

Full history and reasoning for all of the above lives in `CLAUDE.md`
section 15 (decision log) and section 16 (build status table).

**Known gap**: multi-item purchases aren't supported yet. Every
`add_to_cart` call opens its own fresh single-item cart, and there's no way
to check out more than one cart in a single order — see "Known limitations"
below.

## Setup

```bash
uv sync --all-groups
cp .env.example .env   # then fill in your Razorpay TEST-mode keys
```

## Running the server

```bash
uv run mercator
```

Registers three MCP tools: `list_products`, `add_to_cart`, `checkout`.
Requires a valid `.env` (see `.env.example`) — the server refuses to start
if `RAZORPAY_KEY_ID` doesn't look like a test-mode key (`rzp_test_...`), or
if `SPEND_CAP_INR`/`ALLOWED_CATEGORIES` are missing or malformed.

**Transport**: defaults to **stdio** (what Claude Desktop, the MCP
Inspector, and this project's own client tests use). To serve over HTTP
instead — needed to connect a real [Emptor](../Emptor/) instance, whose
client speaks `streamable_http_client` against a URL — set in `.env`:

```
MCP_TRANSPORT=streamable-http
```

The server then listens at `http://127.0.0.1:<MERCATOR_PORT>/mcp` (default
port `8000`). Point Emptor's `MERCATOR_ENDPOINT` at that same URL. This has
been run and verified live end to end — see `CLAUDE.md` section 15,
2026-08-31 entry.

## Running the tests

```bash
uv run pytest
```

## Swapping the catalog

`tests/fixtures/catalog.json` is the default demo catalog, loaded by
`catalog.py` at server startup (`server.DEFAULT_CATALOG_PATH`). Point that
path at a different JSON file to change what Mercator sells — every entry
is sanitized and validated on load; malformed entries are skipped, not
crashed on.

## Audit ledger

Every guardrail check and every checkout result — accepted and rejected —
is appended to `ledger.db`, a Fides hash-chain (`fides.Ledger`, see
`CLAUDE.md` section 9.5) in the repo root. `ledger.db` is gitignored. Call
`Ledger(path).verify_chain()` (a thin passthrough to Fides) to confirm the
chain hasn't been tampered with.

## Known limitations (v1 / demo scope)

- Idempotency store is in-memory — restarting the server loses it.
- Spend cap is per-transaction only, not cumulative.
- **Single-item carts only.** Each `add_to_cart` call opens a fresh cart for
  exactly one product; `checkout` closes out exactly one cart. A client
  that picks multiple distinct products (Emptor's own pipeline does) has no
  way to check them out as a single order against this server today — this
  was confirmed by running the real Emptor agent against this server, not
  just assumed. Resolving it needs a decision: grow this server's cart to
  hold multiple items, or have the client issue one order per item.
- Payment flow stops at order creation — no capture simulation.
