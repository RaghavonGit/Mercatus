"""Start / stop / health-check the Mercator subprocess and the Ollama
endpoint, so a demo operator never opens a terminal.

Ollama cannot be reliably auto-started cross-platform, so it is only
detected and reported (with the fix to run).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from emptor.client import ShopConnection
from emptor.config import load_config

_proc: subprocess.Popen | None = None


async def mercator_reachable(endpoint: str, timeout: float = 3.0) -> bool:
    try:
        async with ShopConnection(endpoint) as session:
            await session.call_tool("list_products", {})
        return True
    except Exception:  # noqa: BLE001
        return False


async def ollama_status(base_url: str, want_models: list[str], timeout: float = 3.0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base_url}/models")
        r.raise_for_status()
        ids = {m.get("id") for m in r.json().get("data", [])}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": type(exc).__name__, "models_present": []}
    return {
        "reachable": True,
        "models_present": [m for m in want_models if m in ids],
        "models_missing": [m for m in want_models if m not in ids],
    }


def start_mercator(mercator_dir: Path) -> None:
    """Spawn `uv run mercator` with streamable-http transport. Idempotent -
    a live process is left alone."""
    global _proc
    if _proc is not None and _proc.poll() is None:
        return
    env = {**os.environ, "MCP_TRANSPORT": "streamable-http"}
    _proc = subprocess.Popen(
        ["uv", "run", "mercator"],
        cwd=str(mercator_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # New process group so we can signal just this tree on shutdown.
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0),
    )


def stop_mercator() -> None:
    global _proc
    if _proc is None:
        return
    if _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None


def mercator_managed_by_us() -> bool:
    return _proc is not None and _proc.poll() is None


async def wait_until_reachable(endpoint: str, deadline_s: float = 25.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if await mercator_reachable(endpoint):
            return True
        await asyncio.sleep(1.0)
    return False


def llm_settings():
    return load_config().llm
