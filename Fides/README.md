# Fides

The tamper-evident trust ledger of the Mercatus project. Fides records
events from any agent-based system as a SHA256 hash-linked chain, and can
prove — not just claim — whether that chain has been altered since it was
written. It is a **library**, not a service: no network interface, no MCP
tools, no HTTP endpoint. Emptor and Mercator import it directly and call it
in-process.

Part of the [Mercatus](../README.md) project — see the root README for how
Emptor, Mercator, and Fides fit together.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Zero runtime
dependencies — standard library only.

```bash
uv sync
```

## Usage

```python
from fides import Ledger

ledger = Ledger("my_app.db")

entry = ledger.log_event(
    {"goal": "birthday gift for a friend who likes hiking", "budget_inr": 1500},
    actor="emptor",
    event_type="goal_received",
)
print(entry.seq, entry.entry_hash)

result = ledger.verify_chain()
if result.is_valid:
    print(f"chain intact, {result.entries_checked} entries")
else:
    print(f"chain broken at seq={result.broken_at_seq}: {result.detail}")
```

For a single shared instance across a process (what Emptor/Mercator's call
sites actually use), the module-level functions bind to a lazily-constructed
default `Ledger`, path from the `FIDES_DB_PATH` environment variable (else
`./fides_ledger.db`):

```python
import fides

fides.log_event({"count": 12}, actor="mercator", event_type="catalog_retrieved")
fides.verify_chain()
```

`log_event` raises `fides.InvalidEntryError` if `actor`/`event_type` are
empty or `data` isn't a JSON-serializable dict — nothing is written on
failure. It raises `fides.LedgerWriteError` if the underlying write fails for
any other reason. Neither is a silent no-op; a caller that doesn't catch
these will see the exception propagate, by design (see "Non-negotiable
design rules" #3 in `CLAUDE.md`).

Run `examples/demo.py` (`uv run python examples/demo.py`) to see a full
run end to end: log events, verify (intact), tamper a row via raw SQL,
verify again (localizes the exact break), write an anchor receipt.

## What Fides proves, and what it doesn't

Every entry includes the SHA256 hash of the entry immediately before it.
Editing any past entry changes its own hash, which no longer matches what
the *next* entry recorded as "the hash of the entry before me" — so
tampering breaks the chain at a specific, provable point. `verify_chain()`
recomputes every hash from the raw stored bytes (never a cache) and reports
either "intact" or exactly which `seq` broke and why.

**Fides proves a chain wasn't edited in place. It does not, by itself, prove
a chain wasn't wholesale replaced** — that requires an external anchor, a
minimal version of which is included (`fides.anchor.write_anchor`). The same
limitation covers **truncation**: deleting one or more entries off the *end*
of the chain leaves a shorter chain that is still fully self-consistent —
`verify_chain()` correctly reports it as intact, because nothing about the
remaining rows was edited. Only comparing the chain's current tail against a
previously-recorded external anchor (a hash written somewhere outside the
database itself) catches either case — `ledger.last_entry_hash()` reads the
current tail's hash live from the store (never cached) for exactly this
comparison; compare it against a previously-written anchor line's
`entry_hash`, not just its `seq` (a same-seq forged replacement could still
match on `seq` alone).

Fides makes no claim of being "unhackable" or "immutable" anywhere in this
codebase. The honest claim is **tamper-evident**: an edit is always
detectable after the fact, not impossible to attempt.

## Known limitations (v1 / demo scope)

- No external anchoring beyond the v1 minimum (`write_anchor` appends a line
  to a local file) — no git-committed hash, no timestamping service, no
  cryptographic signing.
- The `data` field is caller-supplied and unencrypted at rest — fine for a
  local SQLite file used only in a demo, not appropriate for real
  business-sensitive data in production.
- `data` dict keys must be strings by convention (a JSON limitation, not a
  Fides bug) — a dict with non-string keys either fails validation (mixed
  key types aren't orderable under the canonical sort) or gets silently
  coerced to string keys on the round trip through storage.
- `verify_chain()` always walks the full chain from the start; there is no
  range/partial verification. Fine at demo scale.
- The default module-level `Ledger` instance's `close()` is not exposed
  through `fides.log_event`/`fides.verify_chain()` — closing it would break
  it for the rest of the process.

## Never store payment credentials

**Fides has no code-level way to inspect what a caller passes in `data`, so
this is enforced by convention, not by this package.** No card numbers,
CVVs, or raw payment instruments should ever be logged, in any entry, under
any event type, for any reason. This mirrors Emptor's and Mercator's own
rules exactly — consistent across the whole Mercatus family. If you're
integrating Fides into a new caller, treat this as a hard boundary.

## Integration

Fides was built and tested standalone first, exactly like Emptor and
Mercator were, then wired into both — see `CLAUDE.md` section 9 for the
full integration history.

- **Mercator** calls `fides.log_event` (via a thin `ledger.py` adapter) for
  every guardrail check and every checkout result, accepted and rejected.
  Live-verified: a real purchase and a real server-side rejection against
  the real Razorpay API both produced a valid, `verify_chain()`-passing
  chain.
- **Emptor** calls `fides.log_event` at five points: goal received, catalog
  retrieved, LLM decision, validation result, and purchase outcome.
- `event_type` vocabulary in use: `goal_received`, `catalog_retrieved`,
  `llm_decision`, `validation_result`, `guardrail_check`, `checkout_result`,
  `catalog_flagged`.
- Each side logs to its **own** ledger file (`actor="mercator"` /
  `actor="emptor"`) — these are two independent hash chains, not one shared
  chain across both packages. Cross-referencing a purchase across both
  currently means checking both files, not a single combined proof.

## Testing

```bash
uv run pytest -v
```

The suite includes real multi-threaded concurrency tests
(`threading.Thread` + `threading.Barrier`, not sequential calls that happen
to run in the same process) proving `log_event()` is safe under genuine
concurrent access, and a tamper-detection suite that mutates stored rows via
raw SQL (bypassing the API entirely, simulating an attacker with direct DB
access) and asserts `verify_chain()` correctly localizes every break.

```bash
uv run pip-audit
```

`pip-audit` has nothing to scan against runtime dependencies (there are
none) but is run against the dev environment as a matter of discipline.
