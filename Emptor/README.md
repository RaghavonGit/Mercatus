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

## LLM setup

The one LLM call (`decide()` — pick catalog items matching the goal) runs
against a **local model via [Ollama](https://ollama.com)** by default. It
is not the safety boundary (`validate_picks` + the operator prompt + the
human paying the link are), so a small model is fine.

```bash
# once
ollama serve
ollama pull qwen2.5:7b-instruct      # ~4.7 GB, primary
ollama pull qwen2.5:3b-instruct      # ~1.9 GB, fallback for CPU-only / low-VRAM
```

- **GPU:** an 8 GB NVIDIA card runs the 7B comfortably (~1–2 s/decision).
- **CPU-only / <6 GB VRAM:** set `LLM_MODEL=qwen2.5:3b-instruct`.
- If `decide()` gets a non-200 that isn't auth (e.g. the 7B fails to load
  into VRAM), Emptor automatically retries once with `LLM_MODEL_FALLBACK`.
- **Hosted provider instead of Ollama:** set `LLM_BASE_URL` (e.g.
  `https://integrate.api.nvidia.com/v1`), `LLM_MODEL`, and `LLM_API_KEY`.
  Note `response_format: json_object` support is per-model on hosted
  providers; a reasoning model that leaks chain-of-thought may need a
  `decide.py` tweak.

## Configure

```bash
cp .env.example .env
# then edit .env:
#   LLM_BASE_URL / LLM_MODEL / LLM_MODEL_FALLBACK  (defaults = local Ollama)
#   LLM_API_KEY=<only for a hosted provider>
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

`decide()` returns the model's picks **and** a short plain-language
`reasoning` for the choice; that reasoning is written into the
`llm_decision` Fides event (and surfaced by the [Forum demo UI](../Forum/)).
It is display/audit only — it never re-enters validation or the purchase,
and it is model text derived from the untrusted catalog, so treat it as
data, not instructions.

Then prints one of:
- `PENDING: pay INR <total> at <payment_link_url> (link id <id>, expires in ~<N>h)`
  — the checkout went through, but no money has moved yet: a human opens
  that link and pays it. The shop's reconciler records the real final
  outcome afterwards (Emptor has already exited). Exit code 0.
- `SETTLED: bought for INR <total> via <method> -- no payment link to pay,
  the shop settled it` — the shop settled the checkout itself from a balance
  it holds (e.g. Mercator's autopay envelope). Money **has** moved; there is
  nothing to pay. Exit code 0. Emptor does not authorize this — the shop
  decides it deterministically server-side.
- `DECLINED: purchase not confirmed by operator` (stderr) — you didn't type
  `yes`. Not a failure; exit code 0.
- `BLOCKED: <reason>` (stderr, non-zero exit) — Emptor never guesses or
  silently retries a purchase on ambiguity. A common one:
  `BLOCKED: LLM endpoint ... is not reachable ... 'ollama serve'` — the
  preflight check runs before anything else.

### `--picks`: skip the LLM

```bash
uv run emptor "buy stationery" --budget 500 \
  --picks '[{"product_id": "prod_003", "quantity": 1}]'
```

Feeds the picks straight past `decide()` into validation. Still validated
(out-of-catalog / over-budget ids are rejected) and still needs operator
confirmation. For testing and demos when the LLM is unavailable or you want
a deterministic run.

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
needed. The `-m 'not live'` default (see `pyproject.toml`) skips the handful
of `@pytest.mark.live` tests; those hit the configured `LLM_BASE_URL` and
**skip themselves cleanly** if it's unreachable, so `uv run pytest -m live`
is safe to run with or without Ollama up.

To exercise the real pipeline end to end (real MCP connection, real local
LLM decision, real checkout), a minimal stub shop server is included at
`tests/stub_shop/server.py`. It is not Mercator and has no payments — it just
implements the three-tool contract above, backed by
`tests/fixtures/mock_catalog.json`.

### Running against the real Mercator

Emptor has been run live against the real [Mercator](../Mercator/) package —
real MCP connection, real LLM decision, real [Fides](../Fides/) ledger. Start
Mercator with `MCP_TRANSPORT=streamable-http` in its `.env` (see its README),
and make sure `ollama serve` is running, then:

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

Requires `ollama serve` running with `qwen2.5:7b-instruct` pulled — this
makes a real local LLM call.

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
- **The local LLM is a ~5 GB download.** Fine for a workstation; not
  suitable as-is for a thin-client or mobile build. `LLM_BASE_URL` is the
  seam for pointing at a hosted inference endpoint later.

## Security posture

Emptor assumes adversarial catalog input (see `CLAUDE.md` section 6) and
never handles raw payment credentials. Money-safety enforcement is the
shop's responsibility, not Emptor's — Emptor's own budget check is a sanity
check on its own LLM's output, not a security boundary. Run `uv run
pip-audit` before every push.
