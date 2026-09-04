# Forum — the Mercatus demo dashboard

Forum is a local web page that runs the whole Mercatus flow end to end so
you can *watch* it, instead of driving three terminals by hand. It shows:

- the goal + budget you give the shopper
- the shop's catalog and what's within budget
- **which item the local LLM picked, and the reason it gave**
- the shop's independent re-validation of that pick
- the real Razorpay payment link, with its status flipping
  **PENDING → PAID** on its own once you pay it
- both Fides ledgers (Emptor's and Mercator's), live, each with a
  "chain verified" indicator
- a **plain-English recap** of the finished run, above the raw event feed
- the durable spend tracker

Forum is a **presentation layer, not a security boundary.** Every
money-safety rule is still enforced by Mercator; Forum just reuses Emptor's
pipeline steps and reads the other packages' state.

## Prerequisites

- The other three packages present in the monorepo layout
  (`../Emptor`, `../Mercator`, `../Fides`).
- **Ollama** running with the models pulled:
  ```
  ollama serve
  ollama pull qwen2.5:7b-instruct
  ollama pull qwen2.5:3b-instruct
  ```
- `Mercator/.env` set for test mode (`RAZORPAY_MODE=test`,
  `RAZORPAY_KEY_ID`/`_SECRET`, and `MCP_TRANSPORT=streamable-http`).
- `Forum/.env` — copy `.env.example`, paste the **same** test-mode Razorpay
  keys as `Mercator/.env` (Forum needs its own client only to poll a
  payment link's status).

## Run

**One command** (from the repo root) — writes the `.env` files if missing,
funds the autopay envelope, starts Mercator, and opens the browser:

```bash
./demo.sh          # macOS / Linux      (Windows: demo.bat)
```

Headless "does it work?" checklist, no browser / Ollama / keys needed:

```bash
./demo.sh --verify
```

**Or start just the dashboard** (assumes the prerequisites above are set up):

```bash
cd Forum
uv run forum
```

Open <http://127.0.0.1:8100>. The status pills top-right show whether
Mercator and Ollama are up; if Mercator is down, the **Start Mercator**
button spawns it for you (Ollama you start yourself).

For a clean recording (empty ledger panel and a ₹0 spend gauge), delete the
three state files first:

```bash
rm -f Mercator/ledger.db Mercator/spend_tracker.db Forum/forum_ledger.db
```

## Demo walkthrough

1. Type a goal (e.g. *"a fantasy novel for a teenager"*) and a budget, click
   **Shop**.
2. The timeline animates: connect → read catalog → the model chooses (its
   reasoning appears in a quote) → the shop re-checks the pick → a payment
   link is created. No money has moved.
3. Click **Pay now**. On Razorpay's test page use card
   `5267 3181 8797 5449`, any future expiry, any CVV, OTP `1234`.
   (`4111 1111 1111 1111` is rejected as an international card on this
   account.)
4. Back on the dashboard the badge flips **PENDING → PAID** within a few
   seconds — that's Forum polling the link status directly, ahead of
   Mercator's ~45s reconciler.
5. The ledger panel fills in as it goes; after payment Mercator's reconciler
   adds its own `checkout_result` row. Both chain badges stay ✓. The spend
   gauge ticks up.

To skip the LLM, tick **Choose the item myself** and pick from the catalog —
the pick still goes through the shop's validation and the same payment flow.

### The run recap

When a run finishes, the top of the ledger panel shows **what just happened
in plain words**: a small fact box (goal, item, amount, how it settled,
transaction reference, time, whether a human approved) built directly from
the ledger fields, and a short narrative paragraph written by the same local
`qwen2.5:7b-instruct` model reading the committed ledger entries. The figures
and IDs come from code, not the model — an injected product name can only
garble a sentence, never a number. **Show the raw ledger entries** expands the
full hash-chained event list underneath. If Ollama isn't reachable the fact
box still renders and the raw events auto-expand.

This is the only place Forum calls a model besides the pipeline's one
decision call — it reads already-committed data and its output is display
only; it never re-enters validation or purchase. Mercator still makes zero
LLM calls; Emptor still makes exactly one.

### Autopay

If Mercator has autopay enabled (`AUTOPAY_ENABLED=true`, and its envelope
funded via `mercator-topup`), a small purchase in an autopay-eligible
category settles with **no payment link**: the final timeline step reads
*"Settled by autopay"*, a **PAID** card appears instead of the Pay-now card,
and the spend gauge ticks up immediately — Mercator drew a prepaid balance
it holds, no human click. Everything above the threshold or off the stricter
list still creates a normal payment link.

## Known limitations

- **Test mode only.** Forum refuses to start if `RAZORPAY_MODE` isn't
  `test`.
- **No auth, single session.** It's a local demo tool, not a service.
- Emptor and Mercator keep **separate** ledger files (Mercator hardcodes
  its path); Forum shows both rather than one merged chain.
- The catalog is Mercator's static fixture; there's no product management.
- A page reload loses the in-page run state (the ledger + payment status
  are re-read from disk, so they survive).

## Tests

```bash
cd Forum
uv run pytest -q          # smoke tests (stubbed shop, fixture ledger, mocked Razorpay)
```
