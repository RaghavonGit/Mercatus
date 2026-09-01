# Mercatus

An agentic-commerce demo built from three independent, cooperating Python
packages: an AI shopper that decides *what* to buy, a merchant server that
decides *whether* a purchase is allowed, and a tamper-evident ledger that
records what actually happened.

```
Emptor  (buyer)  ──MCP──>  Mercator  (merchant)  ──writes to──>  Fides  (ledger)
   │                              │
   └── one LLM call, ever         └── zero LLM calls, ever — fully deterministic
```

## Components

| Package | Role | Details |
|---|---|---|
| **[Emptor](Emptor/)** | Autonomous shopper agent. Given a goal in plain English and a budget, connects to any MCP-compatible shop, reads its catalog, and decides what to buy via a single LLM call (NVIDIA NIM). Never decides whether a purchase is *allowed*. | [Emptor/README.md](Emptor/README.md) |
| **[Mercator](Mercator/)** | Merchant MCP server. Hosts a catalog and enforces every purchase rule (stock, allowlist, per-transaction + cumulative spend caps, pending-link limit, idempotency) deterministically, server-side. Zero LLM calls. `checkout` creates a Razorpay **Payment Link** the buyer pays themselves; an in-process poller reconciles the outcome. | [Mercator/README.md](Mercator/README.md) |
| **[Fides](Fides/)** | Dependency-free, tamper-evident trust ledger. Records events from Emptor and Mercator as a SHA256 hash-linked chain and can prove — not just claim — whether the chain has been altered. Standard library only. | [Fides/README.md](Fides/README.md) |

## Status

All three packages are independently built and tested (504 tests combined —
334 Mercator, 108 Emptor, 62 Fides). An earlier end-to-end run exercised the
old order-creation path live: a real MCP connection, a real NIM decision, a
real Razorpay test-mode order, a real chain-verified Fides ledger entry, and
a real server-side rejection (over-budget, blocked by the spend-cap
guardrail).

The **real-money upgrade** is now built (Payment Links, cumulative spend
cap, pending-link limit, durable spend tracker, in-process reconciler,
Emptor operator-approval checkpoint). It passes its full mocked test matrix;
a fresh live test-mode verification against the Payment Link flow is the
next step — and `RAZORPAY_MODE=live` stays off until that passes.

**Multi-item purchases** now work end to end: Emptor threads one `cart_id`
across every item and Mercator checks the whole cart out as one Payment
Link.

## Design principle

Money-safety enforcement lives entirely on the merchant side (Mercator).
Emptor is treated as untrusted — even a well-intentioned shopper agent can be
manipulated via prompt injection in catalog data, so every guardrail
(per-transaction + cumulative spend caps, category allowlist, stock,
pending-link limit, idempotency) is re-verified server-side on every call,
fail-closed. Emptor's own operator-approval prompt and its LLM-output sanity
check are *additional* checkpoints, never the safety boundary — the real
boundary is the human who clicks the payment link plus Mercator's
server-side rules.

## Setup

Each package is independently `uv`-managed but **Emptor and Mercator both
depend on Fides via a relative path** (`../Fides`), so all three must be
cloned as siblings, exactly as they appear in this repo:

```bash
git clone <this-repo-url> Mercatus
cd Mercatus/Fides    && uv sync
cd ../Mercator        && uv sync
cd ../Emptor          && uv sync
```

Each package has its own `.env.example` documenting the environment variables
it needs (an NVIDIA NIM key for Emptor, Razorpay test-mode keys for Mercator).
Copy to `.env` in that package's directory and fill in real values — `.env`
files are gitignored and must never be committed.

To run the real pipeline end to end (not the stub shop), set
`MCP_TRANSPORT=streamable-http` in `Mercator/.env`, start it, then point
Emptor at it:

```bash
# terminal 1
cd Mercator && uv run mercator

# terminal 2 (PowerShell)
cd Emptor
$env:MERCATOR_ENDPOINT = "http://127.0.0.1:8000/mcp"
uv run emptor "a fantasy novel to read" --budget 500
# Emptor shows the picks and total, then waits for you to type `yes`.
# It prints a PENDING line with a payment link; open it and pay it in a
# browser, and Mercator's reconciler logs the final outcome within ~45s.
```

See each package's own README for full install, configuration, and usage
details — Mercator's covers the transport flag and the real-money config
(`RAZORPAY_MODE` and the spend/link bounds), Emptor's covers this
walkthrough, the operator-approval prompt, and the `--yes` flag.

## License

MIT — see [LICENSE](LICENSE).
