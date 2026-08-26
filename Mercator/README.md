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
See `CLAUDE.md` section 16 for the full build status table.

Payments have been verified against the **real** Razorpay test-mode API
(not just mocks) — order creation, amount echoing, and idempotency (no
double-charge on a replayed key) all confirmed live. The server has also
been tested through a real MCP client over an actual stdio subprocess (the
official MCP Inspector, and a raw `ClientSession`) — this caught and fixed
a real output-schema bug (see `CLAUDE.md` section 15, 2026-08-25) that
in-process testing alone had missed.

A full-codebase security/correctness audit (2026-08-25) found and fixed 7
issues, including a real overselling bug (stock was never actually
re-verified or reserved at checkout) and a real concurrency race (two
checkout calls for the same last-in-stock item could both succeed) — the
concurrency fix was verified against a real stdio subprocess racing two
genuine concurrent checkout calls, not just in-process tests. See
`CLAUDE.md` section 15, 2026-08-25 audit entry, for the full list.

**Not yet exercised**: end-to-end testing with the actual Emptor package
hasn't been run yet.

## Setup

```bash
uv sync --all-groups
cp .env.example .env   # then fill in your Razorpay TEST-mode keys
```

## Running the server

```bash
uv run mercator
```

Registers three MCP tools over stdio: `list_products`, `add_to_cart`,
`checkout`. Requires a valid `.env` (see `.env.example`) — the server
refuses to start if `RAZORPAY_KEY_ID` doesn't look like a test-mode key
(`rzp_test_...`), or if `SPEND_CAP_INR`/`ALLOWED_CATEGORIES` are missing or
malformed.

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
- Single-item carts only, matching Emptor's current scope.
- Payment flow stops at order creation — no capture simulation.
