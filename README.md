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
| **[Mercator](Mercator/)** | Merchant MCP server. Hosts a catalog and enforces every purchase rule (stock, allowlist, spend cap, idempotency) deterministically, server-side. Zero LLM calls. Creates real Razorpay test-mode orders. | [Mercator/README.md](Mercator/README.md) |
| **[Fides](Fides/)** | Dependency-free, tamper-evident trust ledger. Records events from Emptor and Mercator as a SHA256 hash-linked chain and can prove — not just claim — whether the chain has been altered. Standard library only. | [Fides/README.md](Fides/README.md) |

## Status

All three packages are independently built and tested (350 tests combined —
196 Mercator, 92 Emptor, 62 Fides), and the full pipeline has been run live,
end to end: a real MCP connection, a real NIM decision, a real Razorpay
test-mode order, and a real, chain-verified Fides ledger entry. A real
server-side rejection (an over-budget purchase blocked by Mercator's
spend-cap guardrail) has been verified the same way.

**Known gap**: multi-item purchases aren't supported end to end yet.
Mercator's cart is single-item-only, and there's currently no way to check
out a multi-item pick list as one order — Emptor detects this and fails
loudly with a clear reason rather than mishandling it. See
[Mercator's README](Mercator/README.md#known-limitations-v1--demo-scope)
for details.

## Design principle

Money-safety enforcement lives entirely on the merchant side (Mercator).
Emptor is treated as untrusted — even a well-intentioned shopper agent can be
manipulated via prompt injection in catalog data, so every guardrail
(spend cap, category allowlist, stock, idempotency) is re-verified server-side
on every call, fail-closed.

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
```

See each package's own README for full install, configuration, and usage
details — Mercator's covers the transport flag, Emptor's covers this exact
walkthrough and its one known limitation (single-item purchases only, for
now).

## License

MIT — see [LICENSE](LICENSE).
