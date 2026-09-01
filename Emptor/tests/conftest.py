import pytest

import emptor.run as run_module


@pytest.fixture(autouse=True)
def _no_real_ledger_writes(monkeypatch):
    """None of these tests should touch a real Fides ledger file or the
    module-level singleton (fides.log_event is a lazy, process-global
    instance keyed off FIDES_DB_PATH -- reusing it across tests would leak
    state across tests, and an unstubbed call would create
    ./fides_ledger.db in the working directory during test runs).
    Individual tests override this with their own
    monkeypatch.setattr(run_module, "_safe_log_event", spy) to assert on
    specific logging calls.
    """
    monkeypatch.setattr(run_module, "_safe_log_event", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_real_llm_preflight(monkeypatch):
    """run() calls preflight_llm() before dialing the shop -- on the path of
    almost every test in test_run.py. Unstubbed it makes a real GET to the
    configured LLM endpoint (which, on a dev box with Ollama running, just
    *passes*, silently shipping the coupling). Tests that exercise the
    preflight-failure path override this with their own monkeypatch.
    """
    async def _ok(*a, **k):
        return None

    monkeypatch.setattr(run_module, "preflight_llm", _ok)
