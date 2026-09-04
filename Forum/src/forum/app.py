"""Forum - the Mercatus demo dashboard.

A local FastAPI app + one static page. It drives the real Emptor pipeline
against a real Mercator, shows the local LLM's pick *and its stated reason*,
the validation checks, the Razorpay payment link and its live
pending->paid status, the two Fides ledgers, and the durable spend tracker.

Forum is a presentation layer, **not** a security boundary - that stays in
Mercator. It is allowed to read the other packages' state directly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from forum.config import load_forum_config
from forum.ledger_read import read_ledgers
from forum.pipeline import run_pipeline
from forum import story, system

_STATIC = Path(__file__).resolve().parent / "static"

CONFIG = load_forum_config()
# Emptor's pipeline logs its Fides events to FIDES_DB_PATH; pin it before the
# first log_event so the demo has one stable Emptor-side ledger to read.
os.environ["FIDES_DB_PATH"] = str(CONFIG.emptor_ledger_db)

# Forum's own Razorpay client, for on-demand payment-status checks only.
from mercator.payments import fetch_payment_link, make_client  # noqa: E402
from mercator.spend_tracker import SpendTracker  # noqa: E402

_razorpay = make_client(CONFIG.razorpay_key_id, CONFIG.razorpay_key_secret, CONFIG.razorpay_mode)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    system.stop_mercator()


app = FastAPI(title="Forum - Mercatus demo", lifespan=_lifespan)


@app.middleware("http")
async def _no_store(request, call_next):
    """The dashboard is a live view; never let a browser serve a stale
    index.html / app.js / API response from cache during a demo."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


class RunRequest(BaseModel):
    goal: str
    budget: int
    picks_override: list[dict] | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.post("/api/run")
async def api_run(req: RunRequest) -> StreamingResponse:
    llm = system.llm_settings()

    async def gen():
        try:
            async for event in run_pipeline(
                req.goal.strip(),
                int(req.budget),
                CONFIG.mercator_endpoint,
                llm,
                picks_override=req.picks_override,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'stage': 'blocked', 'at': 'stream', 'reason': str(exc)})}\n\n"
            yield f"data: {json.dumps({'stage': 'done'})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/catalog")
async def api_catalog() -> dict:
    from emptor.client import ShopConnection
    from emptor.discover import discover_catalog

    try:
        async with ShopConnection(CONFIG.mercator_endpoint) as session:
            products = await discover_catalog(session)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"catalog unavailable: {type(exc).__name__}")
    return {"products": products}


@app.get("/api/payment/{link_id}")
def api_payment(link_id: str) -> dict:
    try:
        result = fetch_payment_link(_razorpay, link_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Razorpay lookup failed: {type(exc).__name__}")
    return result


@app.get("/api/ledger")
def api_ledger(after_mercator: int = 0, after_emptor: int = 0) -> dict:
    return read_ledgers(
        CONFIG.mercator_ledger_db,
        CONFIG.emptor_ledger_db,
        after_mercator=after_mercator,
        after_emptor=after_emptor,
    )


# A completed run's narration, keyed by the terminal ledger entry's hash so
# a pending->paid flip re-narrates but a poll does not. Single local session,
# one run at a time -> the cache holds at most one entry.
_story_cache: dict[str, dict] = {}


async def _catalog_names() -> dict[str, str]:
    """product_id -> name, for the story's fact box. Best-effort: if the shop
    is unreachable the story falls back to raw product ids."""
    from emptor.client import ShopConnection
    from emptor.discover import discover_catalog

    try:
        async with ShopConnection(CONFIG.mercator_endpoint) as session:
            products = await discover_catalog(session)
    except Exception:  # noqa: BLE001 - names are a nicety, never a hard dep
        return {}
    return {p["id"]: p.get("name") for p in products if isinstance(p.get("id"), str) and p.get("name")}


@app.get("/api/story")
async def api_story() -> dict:
    # read_ledgers is sync SQLite + verify_chain() over both chains - keep it
    # off the event loop (same reason /api/ledger is a plain `def`).
    ledgers = await asyncio.to_thread(read_ledgers, CONFIG.mercator_ledger_db, CONFIG.emptor_ledger_db)
    names = await _catalog_names()
    facts = story.extract_run_facts(ledgers["events"], names=names)
    if facts is None:
        return {"ready": False}

    key = facts.ledger_ref or "run"
    if key not in _story_cache:
        try:
            prose = await story.narrate(facts, system.llm_settings())
            narrated = True
        except story.StoryError:
            prose, narrated = None, False
        _story_cache.clear()
        _story_cache[key] = {"story": prose, "narrated": narrated}

    entry = _story_cache[key]
    return {
        "ready": True,
        "facts": dataclasses.asdict(facts),
        "story": entry["story"],
        "narrated": entry["narrated"],
    }


@app.get("/api/spend")
def api_spend() -> dict:
    paid_24h = 0
    if CONFIG.mercator_spend_db.exists():
        tracker = SpendTracker(CONFIG.mercator_spend_db)
        try:
            paid_24h = tracker.sum_since(24)
        finally:
            tracker.close()

    env = dotenv_values(CONFIG.mercator_dir / ".env")

    def _int_or_none(key: str) -> int | None:
        raw = (env.get(key) or "").strip()
        return int(raw) if raw.isdigit() else None

    return {
        "paid_inr_24h": paid_24h,
        "per_txn_cap_inr": _int_or_none("SPEND_CAP_INR"),
        "cumulative_cap_inr": _int_or_none("CUMULATIVE_SPEND_CAP_INR"),
        "window_hours": _int_or_none("CUMULATIVE_SPEND_WINDOW_HOURS") or 24,
        "mode": (env.get("RAZORPAY_MODE") or "test").strip(),
    }


@app.get("/api/system/health")
async def api_health() -> dict:
    llm = system.llm_settings()
    return {
        "mercator": {
            "reachable": await system.mercator_reachable(CONFIG.mercator_endpoint),
            "managed_by_forum": system.mercator_managed_by_us(),
            "endpoint": CONFIG.mercator_endpoint,
        },
        "ollama": {
            **await system.ollama_status(llm.base_url, [llm.model, llm.fallback_model]),
            "base_url": llm.base_url,
            "model": llm.model,
        },
    }


@app.post("/api/system/start")
async def api_start() -> dict:
    system.start_mercator(CONFIG.mercator_dir)
    ok = await system.wait_until_reachable(CONFIG.mercator_endpoint, deadline_s=25.0)
    return {"started": ok, "reachable": ok}


app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


def main() -> None:
    import uvicorn

    print(f"Forum -> http://{CONFIG.forum_host}:{CONFIG.forum_port}")
    uvicorn.run(app, host=CONFIG.forum_host, port=CONFIG.forum_port, log_level="warning")
