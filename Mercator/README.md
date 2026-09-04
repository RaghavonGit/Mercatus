# Mercator

The merchant agent of the **Mercatus** project — an MCP server that hosts a
product catalog and enforces every rule about whether a purchase is allowed
to complete. Part of the [Mercatus](../README.md) project — see the root
README for how Emptor, Mercator, and Fides fit together.

Zero LLM calls. Every guardrail is deterministic, server-side, fail-closed.

## Status

All core modules are built and tested: guardrails, idempotency, catalog
sanitization, cart, config, payments, the durable spend tracker, ledger,
and the MCP server itself (three tools: `list_products`, `add_to_cart`,
`checkout`) plus the in-process payment-link reconciler. 437 tests passing.

- **Real-money path (Payment Links)**: `checkout` now creates a Razorpay
  **Payment Link** the buyer pays themselves in a browser — no
  pre-authorized autopay. A background poller reconciles the outcome. The
  full mocked test matrix passes; a live test-mode verification pass (a
  real link, paid by hand) is the next step before `RAZORPAY_MODE=live` is
  ever set.
- **Legacy orders**: `payments.create_order` and its live test-mode
  verification (order creation, amount echoing, replayed-key idempotency,
  all confirmed against the real Razorpay test API) remain in the codebase
  but are no longer on the `checkout` path.
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
- **Autopay (tiered-autonomy envelope)**: an opt-in second checkout path.
  A sub-threshold purchase in a stricter allowlisted category is settled by
  drawing a **prepaid balance** — no human click, no gateway call — while
  everything else falls back to the human-paid Payment Link. Live
  test-mode verified: a real ₹1000 top-up via a hosted link, then a ₹450
  purchase settled from the envelope with zero Razorpay calls, a ₹1200
  purchase correctly fell back to a link, chain stayed valid. See
  `## Autopay` below and `CLAUDE.md` §15.

Full history and reasoning for all of the above lives in `CLAUDE.md`
section 15 (decision log) and section 16 (build status table).

**Multi-item carts** are now supported: `add_to_cart` takes an optional
`cart_id` and merges repeat products into one line; `checkout` closes out
the whole cart in one Payment Link.

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
Requires a valid `.env` (see `.env.example`). The server refuses to start
if any required value is missing or malformed, including:

- `RAZORPAY_MODE` — must be exactly `test` or `live`.
- `RAZORPAY_KEY_ID` — its prefix must match the declared mode
  (`rzp_test_…` for `test`, `rzp_live_…` for `live`); a mismatch either
  direction is a startup failure.
- `SPEND_CAP_INR`, `ALLOWED_CATEGORIES` — as before.

On startup the server also cancels every payment link left in a
`created`/`issued` state from a previous run (they can no longer be
reconciled — this process has no record of which cart they belonged to)
and logs each cancellation to the ledger. **Note:** this cancels *every*
open link on the Razorpay account, not only ones this deployment created.

### Real-money configuration

| Env var | Meaning | Default |
|---|---|---|
| `RAZORPAY_MODE` | `test` or `live` | required |
| `CUMULATIVE_SPEND_CAP_INR` | rolling-window cap on **confirmed-paid** spend; `checkout` is refused (`CUMULATIVE_SPEND_CAP_EXCEEDED`) if `paid_in_window + this_cart` would exceed it. Pending (unpaid) links do **not** count toward it — see the limitation note below | none in test mode; **required** when `live` |
| `CUMULATIVE_SPEND_WINDOW_HOURS` | width of that rolling window | `24` |
| `PAYMENT_LINK_EXPIRE_HOURS` | how long a created link stays payable (stock stays reserved that whole time) | `6` in test mode; **required** when `live` |
| `MAX_PENDING_PAYMENT_LINKS` | `checkout` is refused (`TOO_MANY_PENDING_PAYMENT_LINKS`) while this many links are already awaiting payment | `5` |

Going live forces every real bound to be set deliberately — no falling
back to a test-mode default.

| Env var | Meaning | Default |
|---|---|---|
| `AUTOPAY_ENABLED` | `true` turns on the envelope path; anything other than exact `true`/`false` is a startup failure | `false` |
| `AUTOPAY_THRESHOLD_INR` | a purchase at or under this settles from the envelope (if also category-eligible and single-item). **Must be strictly below `SPEND_CAP_INR`** | required when enabled |
| `AUTOPAY_ALLOWED_CATEGORIES` | comma list; **must be a subset of `ALLOWED_CATEGORIES`** — autopay is always a tightening | required when enabled |
| `AUTOPAY_MAX_BALANCE_INR` | ceiling `mercator-topup` will not let the envelope balance exceed | required when enabled |

## Autopay

A second, opt-in checkout path. When `AUTOPAY_ENABLED=true`, a `checkout`
whose total is **≤ `AUTOPAY_THRESHOLD_INR`**, whose single line item's
category is on **`AUTOPAY_ALLOWED_CATEGORIES`**, is settled by decrementing
a **prepaid rupee balance** held in `spend_tracker.db` — no human click,
no Razorpay call, `status` is already `paid`. Anything else — over
threshold, off the stricter list, multi-item, insufficient balance, any
error — **falls back to the normal human-paid Payment Link** (still
`ok: true`, with an `autopay_fallback_cause`). Autopay never rejects a
checkout that the six guardrails allowed.

Fund the envelope once, in advance, outside the agent flow:

```bash
uv run mercator-topup 1000        # mints a hosted link, waits for you to pay it, then credits ₹1000
uv run mercator-topup 1000 --skip-payment   # credit directly (assumes an out-of-band payment)
```

**Fund the envelope *before* starting `uv run mercator`** — server startup
cancels every open payment link on the account, which would kill an unpaid
top-up link out from under the polling CLI. Deleting `spend_tracker.db`
(e.g. for a clean demo) zeroes the balance, so re-run `mercator-topup`
after any such reset or every autopay purchase will just fall back to a
Payment Link.

The balance is bounded by `AUTOPAY_MAX_BALANCE_INR`; a top-up is money
*in* and never counts toward `CUMULATIVE_SPEND_CAP_INR`, but an autopay
*debit* does (it is money out the door exactly like a paid link).

Every autopay decision writes an `autopay_result` ledger event with an
explicit `human_approval: false` field and a snapshot of the threshold /
allowlist that applied.

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

## Payment-link reconciler

A single daemon thread, started in `main()` before `server.run()`, polls
Razorpay every ~45s for the status of every link still awaiting payment:

- **paid** → records the amount in the durable spend tracker, logs a final
  `checkout_result` (`status: "paid"`), drops the entry.
- **expired** / **cancelled** → releases the stock reservation, logs a
  final `checkout_result` with that status, drops the entry.
- **partially_paid** → logs once (shouldn't happen — links are created
  `accept_partial=false`), keeps waiting.
- **pending** / **unknown** → no action; fails closed toward "keep waiting".

The loop is unkillable: one failed poll is logged to stderr and the next
poll still runs.

## Audit ledger

Every guardrail check and every checkout result — accepted, rejected, and
the reconciler's later final outcomes — is appended to `ledger.db`, a Fides
hash-chain (`fides.Ledger`, see `CLAUDE.md` section 9.5) in the repo root.
`ledger.db` is gitignored. Call `Ledger(path).verify_chain()` (a thin
passthrough to Fides) to confirm the chain hasn't been tampered with.

## Known limitations (v1 / demo scope)

- Idempotency store and the pending-payment-links tracking are in-memory —
  restarting the server loses them.
- **No cross-restart durability for in-flight (pending) purchases.** A
  restart cancels every open payment link (startup cleanup, above); a buyer
  mid-payment must request a fresh checkout. The one thing that *does*
  survive a restart is the cumulative spend total (SQLite
  `spend_tracker.db`) — resetting it would silently widen a real-money cap.
- **The cumulative cap bounds confirmed-paid spend, not total exposure.**
  `already_spent` is the sum of *paid* checkouts in the window (from the
  spend tracker, written only when the reconciler sees `paid`). Links that
  are created but not yet paid contribute nothing to the check, so several
  can be outstanding at once each within the cap. Concurrent exposure is
  bounded separately, by `MAX_PENDING_PAYMENT_LINKS` — worst case is roughly
  `MAX_PENDING_PAYMENT_LINKS × CUMULATIVE_SPEND_CAP_INR` until links resolve.
- Reconciliation is **poll-only** — no Razorpay webhooks.
- **Autopay top-up is poll-in-the-CLI**, not reconciler-backed: if you kill
  `mercator-topup` before the link is paid, the credit is lost (the money
  still reached the merchant account — recoverable by hand). A production
  shape would track pending top-ups durably.
- The legacy `create_order` path has no capture simulation (and is no
  longer wired into `checkout`).
