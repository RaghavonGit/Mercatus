# Autopay — the tiered-autonomy prepaid envelope

> A second, opt-in checkout path for Mercator. A small purchase in a stricter
> allowlisted category is settled from a **prepaid balance Mercator holds** —
> no human click, no gateway call. Everything else falls back to today's
> human-paid Payment Link.
>
> Branch: `feat/mercator-autopay-envelope` · PR #1 · built 2026-09-04.
> Full decision-log detail: `Mercator/CLAUDE.md` §15, `Emptor/CLAUDE.md` §9,
> `Forum/CLAUDE.md` §6 (all gitignored).

---

## 1. What changed

### Mercator

| File | Change |
|---|---|
| `config.py` | New `AutopayConfig` (frozen). `Config` gains `autopay: AutopayConfig \| None = None`. New strict `_require_bool`. `load_config` **raises `ConfigError` at startup** unless `AUTOPAY_THRESHOLD_INR < SPEND_CAP_INR` and `AUTOPAY_ALLOWED_CATEGORIES ⊆ ALLOWED_CATEGORIES`. |
| `autopay_reasons.py` | New, zero-import constants (like `reasons.py`) — the fallback-cause codes. **Not** rejection reasons; `reasons.py` and the tool contract are unchanged. |
| `guardrails.py` | New pure `check_autopay_eligible(total, categories, autopay) -> AutopayEligibility`. Runs *after* all six guardrails + the cumulative cap; **not** in `run_all_guardrails`. Fails closed toward the human path. Single-line-item only in v1. |
| `spend_tracker.py` | Two new tables in the same SQLite file: `autopay_balance` (one row) and `autopay_debits` (`PRIMARY KEY idempotency_key`). `credit_balance()` + `try_autopay_debit()` (one transaction: idempotency claim + conditional balance decrement). `sum_since()` now spans **both** `spend_events` and `autopay_debits` — signature unchanged, still returns `int`. |
| `server.py` | `do_checkout` restructured: guardrails → cumulative cap (moved earlier) → autopay decision (+ durable debit, inside `stock_lock`) → max-pending (human-link path only) → reserve. `CheckoutResult` gains `settled_via` / `autopay_fallback_cause` (both `X \| None`). |
| `ledger.py` | `log_checkout_result` now persists `status` / `payment_link_id` / `amount` / `detail` (was dropping them — **clears `RAZORPAY_MODE=live` blocker (a)**). New `log_autopay_result` with an explicit `human_approval: false` field. |
| `topup.py` + `mercator-topup` script | Fund the envelope. **Not an MCP tool** — `checkout` can't reach it. Mints a real hosted link, polls, credits on `paid`. Enforces `AUTOPAY_MAX_BALANCE_INR`. |

### Emptor

`purchase()` now returns `PendingPurchase | SettledPurchase`. A `SettledPurchase`
(shop settled checkout itself, no link) makes `run.py` print a `SETTLED:` line
and exit 0. **No new LLM call** — Emptor doesn't authorize autopay, it reports
what the shop decided.

### Forum

New `settled` SSE stage. The dashboard shows a "Settled by autopay" timeline
step and a **PAID** card instead of Pay-now; the spend gauge ticks immediately.

### Safety properties

- Zero LLM calls added. Fail-closed toward the human path on anything unexpected.
- Autopay is **structurally** always a tightening of the shop's rules — the
  server won't start otherwise.
- The balance check + decrement + idempotency claim are **one transaction**.
  Genuine multi-threaded tests: 10 threads racing distinct keys never overdraw;
  10 racing one key debit exactly once.
- Idempotency for the autopay path is a durable **SQLite `PRIMARY KEY`**, not the
  in-memory `IdempotencyStore` — a restart-then-replay cannot double-charge.
- Autopay spend **counts toward `CUMULATIVE_SPEND_CAP_INR`**; top-ups never do.
- Every autopay decision writes an `autopay_result` ledger event with
  `human_approval: false` and a snapshot of the threshold/allowlist that applied.

### Still gated

`RAZORPAY_MODE=live` blocker **(b)** — the independent concurrency review — is
now *wider* (this restructured `do_checkout`'s critical section), not closed.
The `MAX_PENDING_PAYMENT_LINKS` race (2026-09-04) is untouched, still open.

---

## 2. Quick start — one command

From the repo root:

```bash
./demo.bat            # Windows
./demo.sh             # macOS / Linux
```

(or `cd Forum && uv run demo`.) It writes the three `.env` files if they are
missing, checks Ollama, tops up the autopay envelope (no payment needed),
starts Mercator, serves the dashboard, and opens your browser at
`http://127.0.0.1:8100`. **Leave the prefilled goal and budget, click "Shop"** —
the shop settles the purchase itself via autopay, no payment link, and the
ledger records `human_approval: false`. `Ctrl+C` stops everything.

Placeholder Razorpay keys are fine — the autopay path makes no gateway calls.
Real Razorpay **test** keys in `Mercator/.env` are only needed for the
payment-link *fallback* path (an over-threshold purchase).

### Just show me it works (no browser, no Ollama, no keys)

```bash
./demo.sh --verify        # or: cd Forum && uv run demo --verify
```

Runs the whole autopay flow in-process and prints a checklist:

```
  PASS  config: threshold below the spend cap, autopay list a subset of the allowlist
  PASS  config: a threshold above the spend cap is refused at startup
  PASS  a 450 rupee books purchase settles via autopay (status: paid, no link)
  PASS  the autopay settle made zero payment-gateway calls
  PASS  the envelope balance went 1000 -> 550
  PASS  the ledger records the autonomous charge with human_approval: false
  PASS  replaying the same idempotency key does not debit a second time
  PASS  a 1200 rupee purchase falls back to a payment link (over the threshold)
  PASS  10 concurrent purchases on a 1000 balance: exactly 3 settle, balance never negative
  PASS  the Fides hash chain is intact

  PASS  (10/10)
```

Everything below is the **manual** path — only needed to run pieces
separately or to change the setup the launcher makes.

---

## 3. Configure (manual)

Add to **`Mercator/.env`** (gitignored — never commit real keys):

```ini
# ... existing keys (RAZORPAY_KEY_ID / _SECRET / RAZORPAY_MODE=test /
#     SPEND_CAP_INR / ALLOWED_CATEGORIES / MERCATOR_PORT / MCP_TRANSPORT) ...

AUTOPAY_ENABLED=true
AUTOPAY_THRESHOLD_INR=800                    # must be strictly < SPEND_CAP_INR
AUTOPAY_ALLOWED_CATEGORIES=books,groceries   # must be a subset of ALLOWED_CATEGORIES
AUTOPAY_MAX_BALANCE_INR=5000
```

With the stock catalog (`tests/fixtures/catalog.json`, 16 products): a
single `books` or `groceries` item at or under ₹800 — "The Hobbit" ₹450, or
any one sandwich ingredient (bread, chicken, lettuce, …) — **settles via
autopay**. Everything else falls back to a link: over ₹800 (Fountain Pen
₹1200), off both allowlists (Bluetooth Speaker ₹3500), or **more than one
line item** — "the things needed for a chicken sandwich" always falls back
on `AUTOPAY_MULTI_ITEM`, however cheap each ingredient is, because
multi-item is checked before threshold or category.

`AUTOPAY_ENABLED` accepts only exact `true` / `false`; anything else (`yes`,
`1`, `True`) is a startup failure. Leave it unset/`false` and Mercator behaves
exactly as before.

---

## 4. Run the full test suites (no services needed)

```bash
cd Mercator && uv run pytest -q      # 437 passed
cd ../Emptor  && uv run pytest -q    # 134 passed, 2 deselected
cd ../Fides   && uv run pytest -q    # 62 passed
cd ../Forum   && uv run pytest -q    # 16 passed  (includes the demo --verify checklist)
```

Autopay-specific selections:

```bash
cd Mercator
uv run pytest -q -k autopay                              # config + guardrail + server
uv run pytest -q tests/test_spend_tracker.py -k concurrent   # the genuine-threads overdraw tests
uv run pytest -q tests/test_topup.py                     # the mercator-topup core
```

---

## 5. Fund the envelope (manual)

> The one-command launcher does this for you. This section is for funding it
> by hand, or with a real hosted payment.

**Do this before starting the Mercator server** — server startup cancels every
open payment link on the account, which would kill an unpaid top-up link.

```bash
cd Mercator
uv run mercator-topup 1000
```

It prints a `rzp.io` URL. Open it in a browser and pay in **test mode**:

| Field | Value |
|---|---|
| Card | `5267 3181 8797 5449` (a domestic test card — `4111 1111 1111 1111` is rejected as international on this account) |
| Expiry | `12/30` (any future date) |
| CVV | `123` (any) |
| "Save this card?" | **Maybe later** |
| OTP | `1234` |

The CLI detects payment within a few seconds and prints
`paid. Envelope balance now 1000.`

**Or credit directly** (skips the hosted payment — for fast iteration):

```bash
uv run mercator-topup 1000 --skip-payment
```

**Check the balance any time:**

```bash
cd Mercator
uv run python -c "from mercator.spend_tracker import SpendTracker as S; \
t=S('spend_tracker.db'); print('balance', t.autopay_balance(), '| sum_since(24)', t.sum_since(24))"
```

---

## 6. Test it manually — pick one path

> The one-command launcher (§2) is Path A with the setup automated. These are
> for running the pieces separately.

### Path A — Forum dashboard (visual, end to end)

```bash
# Terminal 1 — Ollama (once: `ollama pull qwen2.5:7b-instruct`)
ollama serve

# Terminal 2 — Mercator  (Mercator/.env needs MCP_TRANSPORT=streamable-http)
cd Mercator && uv run mercator

# Terminal 3 — Forum   (Forum/.env mirrors Mercator's Razorpay keys)
cd Forum && uv run forum
```

Open <http://127.0.0.1:8100>.

1. **Autopay path:** goal `a fantasy novel for a teenager`, budget `800`, **Shop**.
   Watch: connect → catalog → the model picks *The Hobbit ₹450* (reasoning in the
   quote block) → 4 green validation ticks → the final step reads **"Settled by
   autopay"** and a **PAID** card appears (no Pay-now button). The spend gauge
   ticks up ₹450. The Fides panel shows an `autopay_result` row with
   `"human_approval":false`, `"outcome":"autopay_settled"`, and the
   `balance_before/after`. Both chain badges stay ✓.
2. **Fallback path (over threshold):** goal `a nice fountain pen`, budget `1500`,
   **Shop**. The model picks *Fountain Pen ₹1200* → over the ₹800 threshold →
   the normal **PENDING** payment card appears. The `autopay_result` row shows
   `"outcome":"fell_back_to_manual"`, `"fallback_cause":"AUTOPAY_OVER_THRESHOLD"`.
3. **Fallback path (multi-item):** goal `buy the ingredients I need to make
   a chicken sandwich`, budget `1500`, **Shop**. When the model recognises
   several `groceries` items as relevant (bread, chicken, lettuce, mayo,
   cheese, …) a **PENDING** payment card appears anyway, even though each
   item is individually under the autopay threshold:
   `"fallback_cause":"AUTOPAY_MULTI_ITEM"`. Autopay is single-item only,
   regardless of price or category. **Known unreliable** (live-tested
   2026-09-04): `qwen2.5:7b-instruct` sometimes decides none of the
   groceries are relevant to a named dish and returns an empty pick list
   instead — a genuine model-reasoning limitation on this exact GPU/Ollama
   setup, not a bug in Mercator or Emptor (guardrails/autopay logic is
   unaffected either way; Emptor just reports "empty pick list"). It is
   more reliable at a single named item ("a loaf of bread") than at
   inferring a dish's ingredient list. For a guaranteed multi-item
   demonstration, use Path B's `--picks` below instead of relying on the
   live model.

### Path B — Emptor CLI

```bash
# Terminal 1
cd Mercator && uv run mercator

# Terminal 2  — Emptor/.env needs MERCATOR_ENDPOINT=http://127.0.0.1:8000/mcp
cd Emptor
uv run emptor "a fantasy novel for a teenager" --budget 800 --yes
```

Expect:

```
SETTLED: bought for INR 450 via autopay -- no payment link to pay, the shop settled it
```

Fallback:

```bash
uv run emptor "a nice fountain pen" --budget 1500 --yes
# -> PENDING: pay INR 1200 at https://rzp.io/... (link id plink_..., expires in ~6h)
```

Fallback, guaranteed multi-item (bypasses the LLM's pick, not its judgment —
Emptor still validates each item for real; use this if you need the
chicken-sandwich beat of a demo to land every time):

```bash
uv run emptor "chicken sandwich fixings" --budget 1500 --yes --picks \
  '[{"product_id":"prod_bread","quantity":1},{"product_id":"prod_chicken","quantity":1},
    {"product_id":"prod_lettuce","quantity":1},{"product_id":"prod_tomato","quantity":1},
    {"product_id":"prod_mayo","quantity":1},{"product_id":"prod_cheese","quantity":1}]'
# -> PENDING: pay INR ... (fallback_cause AUTOPAY_MULTI_ITEM in the ledger)
```

Skip the LLM:

```bash
uv run emptor "x" --budget 800 --yes --picks '[{"product_id":"prod_001","quantity":1}]'
```

### Path C — direct, no LLM, no server (fastest; just the Mercator logic)

```bash
cd Mercator
uv run python - <<'PY'
import asyncio, json
from mercator import payments
from mercator.catalog import load_catalog
from mercator.config import load_config
from mercator.ledger import Ledger
from mercator.server import DEFAULT_CATALOG_PATH, DEFAULT_LEDGER_PATH, \
    DEFAULT_SPEND_TRACKER_PATH, build_server, load_env
from mercator.spend_tracker import SpendTracker

async def main():
    cfg = load_config(load_env())
    assert cfg.autopay, "AUTOPAY_ENABLED not set in .env"
    client = payments.make_client(cfg.razorpay_key_id, cfg.razorpay_key_secret, cfg.razorpay_mode)
    tracker = SpendTracker(DEFAULT_SPEND_TRACKER_PATH)
    ledger = Ledger(DEFAULT_LEDGER_PATH)
    srv = build_server(catalog=load_catalog(DEFAULT_CATALOG_PATH), config=cfg,
                       razorpay_client=client, ledger=ledger, spend_tracker=tracker)
    print("balance before:", tracker.autopay_balance())

    add = await srv.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
    cid = add.structured_content["cart_id"]
    out = await srv.call_tool("checkout", {"cart_id": cid, "idempotency_key": "manual-test-1"})
    print("checkout:", json.dumps(out.structured_content))
    print("balance after:", tracker.autopay_balance())
    print("chain:", ledger.verify_chain())
    tracker.close(); ledger.close()

asyncio.run(main())
PY
```

Expect `"settled_via": "autopay"`, `"status": "paid"`, the balance down by 450,
and `is_valid=True`.

---

## 7. Inspect the ledger

```bash
cd Mercator
uv run python - <<'PY'
import sqlite3, json
c = sqlite3.connect("ledger.db")
for seq, actor, et, data in c.execute(
    "select seq,actor,event_type,data from ledger where event_type='autopay_result' order by seq"):
    print(seq, actor, et, json.dumps(json.loads(data), indent=2))
PY

# whole-chain integrity
uv run python -c "from mercator.ledger import Ledger; print(Ledger('ledger.db').verify_chain())"
```

Every autonomous charge is the pair
`"outcome":"autopay_settled"` + `"human_approval":false`.

---

## 8. Reset to a clean state

```bash
# stop the Mercator server first
rm -f Mercator/spend_tracker.db Mercator/ledger.db Forum/forum_ledger.db
# then re-fund the envelope BEFORE starting the server again
cd Mercator && uv run mercator-topup 1000 --skip-payment
```

Deleting `spend_tracker.db` zeroes the envelope — until you re-topup, every
autopay-eligible purchase silently falls back to a payment link.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Mercator won't start: `ConfigError: AUTOPAY_THRESHOLD_INR ... must be strictly below SPEND_CAP_INR` | Lower `AUTOPAY_THRESHOLD_INR` or raise `SPEND_CAP_INR`. |
| `ConfigError: AUTOPAY_ALLOWED_CATEGORIES must be a subset of ALLOWED_CATEGORIES` | Every autopay category must also be in `ALLOWED_CATEGORIES`. |
| `ConfigError: AUTOPAY_ENABLED must be exactly 'true' or 'false'` | Use lowercase `true` / `false`, nothing else. |
| Every purchase falls back to a link even under the threshold | Envelope balance is 0 — run `mercator-topup`. Or the item's category isn't on `AUTOPAY_ALLOWED_CATEGORIES`, or it's multi-item. |
| `mercator-topup` prints a link then `timed out ... nothing credited` | You didn't pay it in time; the balance is untouched. Re-run. |
| `mercator-topup` link is dead / cancelled immediately | You started `uv run mercator` while the top-up link was unpaid — startup cleanup cancelled it. Fund the envelope *before* starting the server. |
| Emptor: `BLOCKED: LLM endpoint ... not reachable ... 'ollama serve'` | Start Ollama and `ollama pull qwen2.5:7b-instruct`. |
| Emptor: MCP schema error / can't connect | `Emptor/.env` needs `MERCATOR_ENDPOINT=http://127.0.0.1:8000/mcp` (with the `/mcp` path) and Mercator needs `MCP_TRANSPORT=streamable-http`. |
| Forum spend gauge didn't move after an autopay settle | It refreshes on the `settled` event; reload the page — `sum_since` includes autopay debits. |
