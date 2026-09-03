# Mercatus

An agentic-commerce demo built from three independent, cooperating Python
packages: an AI shopper that decides *what* to buy, a merchant server that
decides *whether* a purchase is allowed, and a tamper-evident ledger that
records what actually happened.

```
Emptor  (buyer)  ──MCP──>  Mercator  (merchant)  ──writes to──>  Fides  (ledger)
   │                              │
   └── one LLM call, ever         └── zero LLM calls, ever — fully deterministic

Forum  (demo dashboard)  ── drives the whole flow from one web page
```

## Components

| Package | Role | Details |
|---|---|---|
| **[Emptor](Emptor/)** | Autonomous shopper agent. Given a goal in plain English and a budget, connects to any MCP-compatible shop, reads its catalog, and decides what to buy via a single LLM call (a local model via Ollama by default; provider-pluggable). Never decides whether a purchase is *allowed*. | [Emptor/README.md](Emptor/README.md) |
| **[Mercator](Mercator/)** | Merchant MCP server. Hosts a catalog and enforces every purchase rule (stock, allowlist, per-transaction + cumulative spend caps, pending-link limit, idempotency) deterministically, server-side. Zero LLM calls. `checkout` creates a Razorpay **Payment Link** the buyer pays themselves; an in-process poller reconciles the outcome. | [Mercator/README.md](Mercator/README.md) |
| **[Fides](Fides/)** | Dependency-free, tamper-evident trust ledger. Records events from Emptor and Mercator as a SHA256 hash-linked chain and can prove — not just claim — whether the chain has been altered. Standard library only. | [Fides/README.md](Fides/README.md) |
| **[Forum](Forum/)** | Local demo dashboard (FastAPI + one static page). Runs the whole flow from a single screen and shows the catalog, the model's pick **and its stated reason**, the shop's re-validation, the payment link with live PENDING→PAID status, both Fides chains, and the spend tracker. A presentation layer — **not** a security boundary. | [Forum/README.md](Forum/README.md) |

## Status

All four packages are independently built and tested (**542 tests combined**
— 338 Mercator, 128 Emptor, 62 Fides, 14 Forum; plus 2 `@live` Emptor tests
run pre-demo).

The **real-money upgrade** (Payment Links, cumulative spend cap,
pending-link limit, durable spend tracker, in-process reconciler, Emptor
operator-approval checkpoint) and the move of the **decision LLM to a local
model via Ollama** (provider-pluggable; no cloud dependency, no disappearing
preview models) are both **merged and live-verified in Razorpay test mode**:
a real Payment Link paid in a browser flips to `paid` on its own, the
reconciler records it, the spend tracker increments, and both Fides chains
stay verified. `RAZORPAY_MODE=live` remains gated on two hardening items
(see `Mercator/CLAUDE.md`).

The decision call now also returns the model's **reasoning** for its pick,
recorded in the `llm_decision` ledger event and shown by Forum.

**Multi-item purchases** work end to end: Emptor threads one `cart_id`
across every item and Mercator checks the whole cart out as one Payment
Link.

## Demo UI

For a watchable run — or for anyone who isn't going to use a terminal —
**[Forum](Forum/)** serves a dashboard:

```bash
cd Forum && uv run forum      # -> http://127.0.0.1:8100
```

It starts Mercator for you, runs the pipeline with a live-animating
timeline, shows the model's pick and its reason, opens the real Razorpay
test-mode link, and flips the status to **PAID** on its own once you pay it.
See [Forum/README.md](Forum/README.md) for the walkthrough.

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
cd ../Forum           && uv sync   # optional — the demo dashboard
```

Each package has its own `.env.example` documenting the environment
variables it needs (Emptor defaults to a local Ollama model — no key;
Mercator needs Razorpay test-mode keys). Copy to `.env` in that package's
directory and fill in real values — `.env` files are gitignored and must
never be committed.

Emptor's decision step needs a local model: install
[Ollama](https://ollama.com), then `ollama pull qwen2.5:7b-instruct`
(see [Emptor/README.md](Emptor/README.md#llm-setup)).

To run the real pipeline end to end (not the stub shop), set
`MCP_TRANSPORT=streamable-http` in `Mercator/.env`, start it, then point
Emptor at it:

```bash
# terminal 0
ollama serve

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

Or skip the three terminals and use the dashboard — see **[Demo UI](#demo-ui)**.

See each package's own README for full details — Mercator's covers the
transport flag and the real-money config (`RAZORPAY_MODE`, the spend/link
bounds), Emptor's covers the LLM setup, this walkthrough, the
operator-approval prompt, `--yes`, and `--picks`, Forum's covers the
dashboard and its demo walkthrough.

## License

MIT — see [LICENSE](LICENSE).
