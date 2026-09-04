"""``uv run demo`` -- one command that sets everything up and opens the
Mercatus dashboard in a browser. No terminals, no .env editing.

    cd Forum && uv run demo             # set up + launch + open the browser
    cd Forum && uv run demo --verify    # headless: run the autopay flow, print a PASS/FAIL checklist
    cd Forum && uv run demo --fund 3000 # set a different starting envelope balance

From the repo root the wrappers ``demo.bat`` / ``demo.sh`` do the ``cd`` for you.

What the default run does:
  1. writes Mercator/.env, Emptor/.env, Forum/.env if they are missing
     (placeholder Razorpay test keys are fine -- the autopay path makes no
     gateway calls; real test keys only matter for the payment-link fallback)
  2. checks Ollama is up with the decision model pulled
  3. tops up the autopay envelope (no payment needed)
  4. starts Mercator, waits for it, then serves Forum and opens the browser
  5. Ctrl+C stops everything
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MERCATOR = _REPO_ROOT / "Mercator"
_EMPTOR = _REPO_ROOT / "Emptor"
_FORUM = _REPO_ROOT / "Forum"

_PLACEHOLDER_KEY_ID = "rzp_test_demoplaceholder"
_PLACEHOLDER_SECRET = "demo_placeholder_secret"

_MERCATOR_ENV = """\
# Auto-written by `uv run demo`. Replace the two keys with your own Razorpay
# TEST-mode credentials (Dashboard -> Settings -> API Keys -> Test Mode) if you
# want the payment-link fallback path to work; the autopay path needs no keys.
RAZORPAY_KEY_ID={key_id}
RAZORPAY_KEY_SECRET={secret}
RAZORPAY_MODE=test
SPEND_CAP_INR=1500
ALLOWED_CATEGORIES=books,toys,stationery
MERCATOR_PORT=8000
MCP_TRANSPORT=streamable-http

AUTOPAY_ENABLED=true
AUTOPAY_THRESHOLD_INR=800
AUTOPAY_ALLOWED_CATEGORIES=books
AUTOPAY_MAX_BALANCE_INR=20000
"""

_EMPTOR_ENV = """\
# Auto-written by `uv run demo`.
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen2.5:7b-instruct
LLM_MODEL_FALLBACK=qwen2.5:3b-instruct
MERCATOR_ENDPOINT=http://127.0.0.1:8000/mcp
DEFAULT_BUDGET_INR=1500
FIDES_DB_PATH=./fides_ledger.db
"""

_FORUM_ENV = """\
# Auto-written by `uv run demo`. Mirrors Mercator/.env's Razorpay keys.
RAZORPAY_KEY_ID={key_id}
RAZORPAY_KEY_SECRET={secret}
RAZORPAY_MODE=test
FORUM_HOST=127.0.0.1
FORUM_PORT=8100
"""

_OLLAMA_URL = "http://127.0.0.1:11434"
_WANT_MODEL = "qwen2.5:7b-instruct"


def _say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------
# setup


def _ensure_envs() -> tuple[bool, str]:
    """Create the three .env files if missing; make sure Mercator's has the
    autopay block. Returns (using_real_keys, key_id)."""
    existing = _read_env(_MERCATOR / ".env")
    key_id = existing.get("RAZORPAY_KEY_ID", "").strip()
    secret = existing.get("RAZORPAY_KEY_SECRET", "").strip()
    real = key_id.startswith("rzp_test_") and key_id != _PLACEHOLDER_KEY_ID and bool(secret)
    if not real:
        key_id, secret = _PLACEHOLDER_KEY_ID, _PLACEHOLDER_SECRET

    if not (_MERCATOR / ".env").exists():
        (_MERCATOR / ".env").write_text(_MERCATOR_ENV.format(key_id=key_id, secret=secret), encoding="utf-8")
        _say(f"wrote Mercator/.env  ({'your keys' if real else 'placeholder keys'})")
    elif "AUTOPAY_ENABLED" not in existing:
        with (_MERCATOR / ".env").open("a", encoding="utf-8") as fh:
            fh.write(
                "\n# added by `uv run demo`\nAUTOPAY_ENABLED=true\n"
                "AUTOPAY_THRESHOLD_INR=800\nAUTOPAY_ALLOWED_CATEGORIES=books\n"
                "AUTOPAY_MAX_BALANCE_INR=20000\n"
            )
        _say("added the autopay block to your existing Mercator/.env")
    else:
        _say(f"Mercator/.env already set  ({'your keys' if real else 'placeholder keys'})")

    if not (_EMPTOR / ".env").exists():
        (_EMPTOR / ".env").write_text(_EMPTOR_ENV, encoding="utf-8")
        _say("wrote Emptor/.env")

    if not (_FORUM / ".env").exists():
        (_FORUM / ".env").write_text(_FORUM_ENV.format(key_id=key_id, secret=secret), encoding="utf-8")
        _say("wrote Forum/.env")

    return real, key_id


def _ensure_ollama(deadline_s: float = 90.0) -> bool:
    import httpx

    start = time.monotonic()
    warned = False
    while time.monotonic() - start < deadline_s:
        try:
            r = httpx.get(f"{_OLLAMA_URL}/api/tags", timeout=3.0)
            names = {m.get("name", "").split(":")[0] for m in r.json().get("models", [])}
            full = {m.get("name", "") for m in r.json().get("models", [])}
            if _WANT_MODEL in full or "qwen2.5" in names:
                _say("Ollama is up, decision model present")
                return True
            _say(f"Ollama is up but {_WANT_MODEL} is missing -- run:  ollama pull {_WANT_MODEL}")
            return False
        except Exception:
            if not warned:
                _say("Ollama not reachable. In another terminal run:  ollama serve")
                _say(f"(first time also:  ollama pull {_WANT_MODEL})   -- waiting up to 90s...")
                warned = True
            time.sleep(3.0)
    _say("gave up waiting for Ollama.")
    return False


def _fund_envelope(amount: int) -> None:
    from mercator.spend_tracker import SpendTracker

    envd = _read_env(_MERCATOR / ".env")
    try:
        ceiling = int(envd.get("AUTOPAY_MAX_BALANCE_INR", "20000"))
    except ValueError:
        ceiling = 20000
    target = min(amount, ceiling)

    db = _MERCATOR / "spend_tracker.db"
    current = 0
    if db.exists():
        t = SpendTracker(db)
        current = t.autopay_balance()
        t.close()
    if current >= target:
        _say(f"envelope already funded (balance {current})")
        return

    topup = target - current
    _say(f"funding the autopay envelope with {topup} (no payment needed)...")
    # Drop VIRTUAL_ENV so the nested `uv run` targets Mercator's own venv cleanly.
    child_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    r = subprocess.run(
        ["uv", "run", "mercator-topup", str(topup), "--skip-payment"],
        cwd=str(_MERCATOR),
        capture_output=True,
        text=True,
        env=child_env,
    )
    if r.returncode != 0:
        _say(f"top-up failed: {(r.stderr or r.stdout).strip()}")
        raise SystemExit(1)
    _say(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else f"envelope funded to {target}")


# --------------------------------------------------------------------------
# interactive launch


def _launch(open_browser: bool) -> int:
    from forum import system
    from forum.config import load_forum_config

    cfg = load_forum_config()
    url = f"http://{cfg.forum_host}:{cfg.forum_port}"

    _say("starting Mercator...")
    system.start_mercator(cfg.mercator_dir)
    ok = asyncio.run(system.wait_until_reachable(cfg.mercator_endpoint))
    _say("Mercator is up" if ok else "Mercator did not come up in time -- the dashboard has a Start button")

    print()
    print("=" * 64)
    print(f"  Mercatus demo is ready:   {url}")
    print("  In the page: leave the prefilled goal + budget, click 'Shop'.")
    print("  You'll see the shop settle the purchase itself via autopay -")
    print("  no payment link, and the ledger records 'human_approval: false'.")
    print("  Ctrl+C here stops everything.")
    print("=" * 64)
    print()

    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    import uvicorn

    from forum.app import app

    try:
        uvicorn.run(app, host=cfg.forum_host, port=cfg.forum_port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        _say("stopping Mercator...")
        system.stop_mercator()
    return 0


# --------------------------------------------------------------------------
# headless verification


def _verify() -> int:
    import copy
    from unittest.mock import Mock

    from mercator.catalog import load_catalog
    from mercator.config import load_config
    from mercator.ledger import Ledger
    from mercator.server import DEFAULT_CATALOG_PATH, build_server
    from mercator.spend_tracker import SpendTracker

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mercatus-demo-verify-"))
    results: list[tuple[bool, str]] = []

    def check(cond: bool, label: str) -> None:
        results.append((bool(cond), label))
        print(f"  {'PASS' if cond else 'FAIL'}  {label}", flush=True)

    env = {
        "RAZORPAY_KEY_ID": "rzp_test_verify", "RAZORPAY_KEY_SECRET": "s", "RAZORPAY_MODE": "test",
        "SPEND_CAP_INR": "1500", "ALLOWED_CATEGORIES": "books,toys,stationery",
        "AUTOPAY_ENABLED": "true", "AUTOPAY_THRESHOLD_INR": "800",
        "AUTOPAY_ALLOWED_CATEGORIES": "books", "AUTOPAY_MAX_BALANCE_INR": "100000",
    }
    config = load_config(env)
    check(config.autopay is not None
          and config.autopay.threshold_inr < config.spend_cap_inr
          and set(config.autopay.allowed_categories) <= set(config.allowed_categories),
          "config: threshold below the spend cap, autopay list a subset of the allowlist")

    bad = dict(env, AUTOPAY_THRESHOLD_INR="2000")
    try:
        load_config(bad)
        check(False, "config: a threshold above the spend cap is refused at startup")
    except Exception:
        check(True, "config: a threshold above the spend cap is refused at startup")

    def fake_client():
        c = Mock()
        c.payment_link.create.return_value = {
            "id": "plink_demo", "short_url": "https://rzp.io/i/plink_demo", "status": "created",
        }
        c.payment_link.all.return_value = {"payment_links": []}
        c.payment_link.fetch.return_value = {"status": "created", "amount": 0, "amount_paid": 0}
        return c

    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    client = fake_client()
    tracker = SpendTracker(tmp / "spend.db")
    tracker.credit_balance(1000)
    ledger = Ledger(tmp / "ledger.db")
    server = build_server(catalog=copy.deepcopy(catalog), config=config,
                          razorpay_client=client, ledger=ledger, spend_tracker=tracker)

    import json as _j

    from forum import story as _story

    def _story_events() -> list[dict]:
        """The ledger rows so far, shaped like ledger_read.read_ledgers, with a
        synthetic emptor goal_received in front (the emptor pipeline isn't run
        here) so the dashboard's fact extractor has a run to anchor on."""
        evs = [{
            "seq": 0, "timestamp": "2026-09-04T09:59:59+00:00", "actor": "emptor",
            "event_type": "goal_received",
            "data": {"goal": "a fantasy novel for a teenager", "budget_inr": 800}, "entry_hash": "seed",
        }]
        for r in ledger._ledger._store.get_all_entries():
            evs.append({
                "seq": r["seq"], "timestamp": r["timestamp"], "actor": r["actor"],
                "event_type": r["event_type"], "data": _j.loads(r["data"]),
                "entry_hash": r["entry_hash"][:12],
            })
        return evs

    async def run_checks():
        add = await server.call_tool("add_to_cart", {"product_id": "prod_001", "quantity": 1})
        cid = add.structured_content["cart_id"]
        out = (await server.call_tool("checkout", {"cart_id": cid, "idempotency_key": "verify-1"})).structured_content
        check(out.get("settled_via") == "autopay" and out.get("status") == "paid",
              "a 450 rupee books purchase settles via autopay (status: paid, no link)")
        check(client.payment_link.create.call_count == 0,
              "the autopay settle made zero payment-gateway calls")
        check(tracker.autopay_balance() == 550,
              "the envelope balance went 1000 -> 550")

        # the dashboard's plain-English run story, from the real ledger rows
        # written so far (no LLM -- just the fact extractor)
        rf = _story.extract_run_facts(_story_events(), names={"prod_001": "The Hobbit"})
        check(rf is not None and rf.outcome == "settled"
              and rf.human_approval is False and rf.amount_inr == 450,
              "the dashboard's run story reads the ledger: The Hobbit, INR 450, no human approval")

        ap = next((_j.loads(r["data"]) for r in ledger._ledger._store.get_all_entries()
                   if r["event_type"] == "autopay_result"), None)
        check(ap is not None and ap.get("human_approval") is False and ap.get("outcome") == "autopay_settled",
              "the ledger records the autonomous charge with human_approval: false")

        replay = (await server.call_tool("checkout", {"cart_id": cid, "idempotency_key": "verify-1"})).structured_content
        check(replay == out and tracker.autopay_balance() == 550,
              "replaying the same idempotency key does not debit a second time")

        add2 = await server.call_tool("add_to_cart", {"product_id": "prod_003", "quantity": 1})  # 1200, stationery
        cid2 = add2.structured_content["cart_id"]
        fb = (await server.call_tool("checkout", {"cart_id": cid2, "idempotency_key": "verify-2"})).structured_content
        check(fb.get("payment_link_id") == "plink_demo"
              and fb.get("autopay_fallback_cause") == "AUTOPAY_OVER_THRESHOLD",
              "a 1200 rupee purchase falls back to a payment link (over the threshold)")

    asyncio.run(run_checks())

    # concurrency: the atomic primitive under real threads
    ct = SpendTracker(tmp / "conc.db")
    ct.credit_balance(1000)  # covers exactly 3 of the 10 x300 debits
    statuses: dict[int, str] = {}
    barrier = threading.Barrier(10)

    def worker(i: int) -> None:
        barrier.wait()
        statuses[i] = ct.try_autopay_debit(f"k{i}", 300).status

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(list(statuses.values()).count("committed") == 3
          and ct.autopay_balance() == 100 and ct.autopay_balance() >= 0,
          "10 concurrent purchases on a 1000 balance: exactly 3 settle, balance never negative")

    check(ledger.verify_chain().is_valid, "the Fides hash chain is intact")

    tracker.close(); ledger.close(); ct.close()
    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print()
    print(f"  {'PASS' if passed == total else 'FAIL'}  ({passed}/{total})")
    return 0 if passed == total else 1


# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(prog="demo", description="One-command Mercatus autopay demo.")
    ap.add_argument("--verify", action="store_true",
                    help="headless: run the autopay flow and print a PASS/FAIL checklist, then exit")
    ap.add_argument("--fund", type=int, default=5000, help="starting envelope balance (default 5000)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = ap.parse_args()

    print("Mercatus demo")
    if args.verify:
        raise SystemExit(_verify())

    real, _ = _ensure_envs()
    if not real:
        _say("note: placeholder Razorpay keys -- autopay works; the payment-link")
        _say("      fallback path needs real test keys in Mercator/.env.")
    if not _ensure_ollama():
        _say("Start Ollama, then re-run  `uv run demo`.")
        raise SystemExit(1)
    _fund_envelope(args.fund)
    raise SystemExit(_launch(open_browser=not args.no_browser))


if __name__ == "__main__":
    main()
