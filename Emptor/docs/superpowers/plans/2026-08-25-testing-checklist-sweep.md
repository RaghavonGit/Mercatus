# Testing Checklist Sweep — Implementation Plan

> **For agentic workers:** This plan is executed inline in the same session (not subagent-driven) — see "Why inline" below.

**Goal:** Close every `⬜` in `docs/TESTING_CHECKLIST.md`: write the missing T1/T2 tests, resolve the 4 deferred Minor findings, run T3 (live NIM + stub-shop E2E), and update the checklist/decision log to match reality.

**Spec:** `docs/TESTING_CHECKLIST.md` (the checklist itself — IDs and expected behavior are already fully specified there; this plan only maps IDs to concrete decisions and file edits, it does not duplicate test code).

**Why inline, not subagent-driven or step-by-step TDD blocks:** advisor review flagged that the checklist already *is* the spec, and CLAUDE.md §0.6 puts well-specified implementation work on Sonnet directly. Writing every test's code into this plan and then again into the test file duplicates effort for no review benefit, and a fresh subagent would just re-read the 8 source + 8 test files already in context. `validate.py` is the one file warranting extra care (it's the security backstop) — that's handled by care during inline execution, not by delegation.

## Reconciliation — checklist is stale before we start

The checklist header still says 64 tests / commit `b2bf9e8`, but the following are already implemented and just mis-marked ⬜ in the copy at rest:
- **VAL-10** → `test_validate_picks_rejects_empty_list`
- **CFG-05** → `test_load_config_invalid_budget_raises`
- **DEC-15** → `test_decide_caps_max_tokens`
- **DIS-13** → `test_discover_catalog_raises_when_not_a_list`
- **CLI-05** — actually ⬜ (doc says 🔶 "written but not run"; no such test exists in `test_client.py`)

These get flipped to ✅ in the checklist doc without new code. Everything else genuinely absent gets written below.

## Design decisions locked before writing tests

1. **CFG-06** ("no shop endpoint literal anywhere in `src/`") — as literally written this contradicts CFG-03 (`config.py` must default to `http://localhost:8000`) and would also flag `decide.py`'s NIM URL, which isn't the shop endpoint at all. Test the *intent* of rule #3 instead: `MERCATOR_ENDPOINT` env var round-trips into `Config.mercator_endpoint`, proving the shop target is configurable, not that zero URL literals exist anywhere.
2. **VAL-19** ("validates against the original catalog") — `run.py` passes `validate_picks` the *filtered* list (`affordable`), not the pre-filter catalog, and `pre_filter` doesn't copy items. There is no separate "original catalog" snapshot in the current design. Ruling: `validate_picks` validates against whatever catalog list it is handed — that's its actual contract — and rule #2 (shop is the money-safety authority, Emptor's check is a sanity check on its own LLM output) makes this an acceptable scope. Test what's true: a pick against a product not present in the list passed to `validate_picks` is rejected, even if that product exists in some other (e.g. unfiltered) catalog list. Record this as a Decision Log entry, not silently.
3. **CFG-07** — mask the API key from `repr()`. Implementation: `field(repr=False)` on `Config.nim_api_key`. Test: `str(config.nim_api_key)` (the real value) does not appear in `repr(config)`.
4. **PUR-08** — the "no order_id" branch in `purchase.py` is the one that matters (money already moved via `add_to_cart`/`checkout`) and currently omits the idempotency key. Add it to that message only (the `checkout failed` branch already includes it via `is_error`... actually check: currently neither includes it except the raise at line 50 which does via f-string `idempotency_key=`). Fix: include `idempotency_key` in the no-order_id message too, for manual reconciliation.
5. **DIS-15** — `Product.price_inr` is typed `int` but `discover.py` accepts `int | float`. Ruling: **keep accepting float**, for shop-agnostic graceful degradation (a shop server might reasonably serialize `899.0`). Cost if wrong: a float price propagates into `ValidatedItem.line_total_inr` / `ValidationResult.total_inr` (both typed `int`, both used in arithmetic and in the `INR {total}` success line) — Python won't crash on this (int/float arithmetic silently upcasts), but a fractional-rupee total would look wrong in output. This is a cosmetic/typing-consistency gap, not a money-safety gap (rule #2's real backstop is shop-side). Fix: relax `Product.price_inr`'s type hint to `int | float` so the TypedDict matches what the validator actually accepts, and write DIS-15 to lock in current (accepting) behavior. No behavior change, just making the type honest.
6. **SEC-09** — `.claude/` (local runtime lock file) → add to `.gitignore`. `.python-version` and `docs/` (includes this plan and the checklist) → commit, they're real project artifacts.
7. **SEC-10** — `scratch_live_decide.py` confirmed absent from the working tree (`find` returned nothing). Mark done, no action needed.
8. **README gap** — README's "Shop server contract" section doesn't mention `MAX_DISTINCT_ITEMS=10` / `MAX_QTY_PER_ITEM=10`. Add a short note.

## Known test-authoring traps (from advisor review — avoid re-deriving these)

- **PUR-09**: the existing `_FakeSession` in `test_purchase.py` keys canned responses by tool name, so every `add_to_cart` call returns the same result — can't express "item 2 of 3 fails" with it. Needs a small per-call fake (e.g. a list of responses popped by call index, keyed by name) local to that test.
- **RUN-10**: exercises `main()`, which wraps `run()` in `asyncio.run`. Monkeypatching `run_module.run` with a plain (non-async) function makes `asyncio.run` raise `TypeError`, and the test would "pass" for the wrong reason if not checked carefully — use an async fake that records the `budget_inr` it was called with.
- **Pytest `live` marker**: registering it in `pyproject.toml` only silences the unknown-mark warning — `testpaths` still collects marked tests, so a clean checkout with no `NIM_API_KEY` would fail T1/T2-only runs. Need `addopts = "-m 'not live'"` so `uv run pytest -v` (no `-m`) skips them by default, and `-m live` opts back in.
- **T3 scope split**: `DEC-L3`/`DEC-L4` need only a real key → real `@pytest.mark.live` pytest tests. `E2E-01`..`E2E-04` need a running stub server → manual runs (start server, run CLI, record output), not managed inside pytest — avoids a flaky suite that owns a subprocess lifecycle.
- **E2E-03** needs checkout to *fail*, but the stub always succeeds on a non-empty cart. Don't edit `tests/stub_shop/server.py` in place — copy it into the scratchpad with `checkout` forced to raise, run that copy for E2E-03 only.
- **E2E-04** needs an injected-catalog run. Don't edit `tests/fixtures/mock_catalog.json` (unit tests assert its exact contents) or `tests/stub_shop/server.py`. Write a new catalog JSON (scratchpad or `tests/stub_shop/`) and point `STUB_CATALOG_PATH` at it.
- The stub server holds `_cart`/`_orders` as module globals and raises on an empty-cart checkout — residue carries across runs within one server process. Restart the server between E2E cases that end without a successful checkout.
- `load_dotenv()` doesn't override already-set env vars, so a shell-exported `$env:MERCATOR_ENDPOINT` wins over `.env`'s value — worth confirming once rather than assuming, since a stale `.env` would silently misdirect a run.
- `DEC-L3`/`DEC-L4` are live-model-behavior observations, not deterministic assertions — record what the model actually returned; the property that must hold deterministically (in-catalog, in-budget) is already enforced by Step 5 regardless of what the model does.

## Execution order

1. Reconcile checklist statuses (no code) — flip the 5 already-covered ⬜ rows to ✅.
2. `filters.py`: FIL-07, FIL-08.
3. `validate.py`: VAL-06, VAL-13, VAL-18, VAL-19 (per ruling above).
4. `config.py`: CFG-06 (per ruling), CFG-07 (fix + test), CFG-08 already done.
5. `discover.py`: DIS-04, DIS-14, DIS-15 (type fix + test).
6. `purchase.py`: PUR-08 (fix + test), PUR-09 (per-call fake).
7. `decide.py`: DEC-13, DEC-14.
8. `run.py`: RUN-09, RUN-10.
9. `client.py`: CLI-05 (bounded-time failure against an unreachable endpoint).
10. pytest config: register `live` marker + `addopts = "-m 'not live'"` in `pyproject.toml`.
11. Repo hygiene: gitignore `.claude/`, leave `.python-version`/`docs/` to be committed when the user asks; confirm SEC-01..SEC-10 (already spot-checked clean).
12. README: document the sanity caps.
13. Run full `uv run pytest -v` (T1+T2), fix anything red.
14. T3: `-m live` pytest tests (DEC-L3, DEC-L4) against the real NIM key already in `.env`.
15. T3: E2E-01/02/03/04 manual runs against `tests/stub_shop/server.py` (plus a scratch checkout-fails copy for E2E-03, plus an injection catalog for E2E-04). Record actual output for each.
16. Update `docs/TESTING_CHECKLIST.md` §13 coverage matrix and §8-equivalent status, and append the ruling entries (items 2, 5, 6 above at minimum) to `CLAUDE.md`'s Decision Log.
17. Do not commit — user said don't commit until asked.
