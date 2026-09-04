"""Public API tests: log_event()/verify_chain() normal-path behavior.

Covers CLAUDE.md section 11 checklist items:
- First entry has prev_hash=NULL, chain still verifies
- A chain of N entries verifies correctly when untouched
- Two threads calling log_event() concurrently -> resulting chain still
  verifies, no forked prev_hash
- log_event() with non-JSON-serializable data -> raises clearly, nothing
  partially written
- log_event() with empty actor or event_type -> raises clearly
"""

import threading

import pytest

import fides
import fides.ledger as ledger_module
from fides.ledger import InvalidEntryError, Ledger


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger(tmp_path / "test.db")
    yield lg
    lg.close()


def test_normal_append_grows_the_chain(ledger):
    e1 = ledger.log_event({"goal": "gift"}, actor="emptor", event_type="goal_received")
    e2 = ledger.log_event({"count": 5}, actor="mercator", event_type="catalog_retrieved")

    assert e1.seq == 1 and e2.seq == 2
    assert e1.prev_hash is None
    assert e2.prev_hash == e1.entry_hash


def test_first_entry_prev_hash_none_still_verifies(ledger):
    ledger.log_event({"goal": "gift"}, actor="emptor", event_type="goal_received")
    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 1


def test_untouched_chain_of_n_entries_verifies(ledger):
    for i in range(10):
        ledger.log_event({"i": i}, actor="emptor", event_type="goal_received")

    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 10
    assert result.broken_at_seq is None
    assert result.reason is None


def test_empty_chain_verifies_as_valid(ledger):
    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 0


@pytest.mark.parametrize("bad_actor", ["", "   ", None, 123])
def test_empty_or_invalid_actor_raises(ledger, bad_actor):
    with pytest.raises(InvalidEntryError):
        ledger.log_event({"x": 1}, actor=bad_actor, event_type="goal_received")


@pytest.mark.parametrize("bad_event_type", ["", "   ", None, 123])
def test_empty_or_invalid_event_type_raises(ledger, bad_event_type):
    with pytest.raises(InvalidEntryError):
        ledger.log_event({"x": 1}, actor="emptor", event_type=bad_event_type)


@pytest.mark.parametrize("bad_data", [["not", "a", "dict"], "a string", None, 42])
def test_non_dict_data_raises(ledger, bad_data):
    with pytest.raises(InvalidEntryError):
        ledger.log_event(bad_data, actor="emptor", event_type="goal_received")


def test_non_serializable_data_raises_and_writes_nothing(ledger):
    ledger.log_event({"a": 1}, actor="emptor", event_type="goal_received")
    ledger.log_event({"b": 2}, actor="emptor", event_type="goal_received")

    rows_before = ledger._store.get_all_entries()
    assert len(rows_before) == 2

    class NotSerializable:
        pass

    with pytest.raises(InvalidEntryError):
        ledger.log_event({"x": NotSerializable()}, actor="emptor", event_type="goal_received")

    rows_after = ledger._store.get_all_entries()
    assert len(rows_after) == 2, "a rejected entry must not partially write"
    assert [r["seq"] for r in rows_after] == [r["seq"] for r in rows_before]

    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 2


def test_nan_data_raises_and_writes_nothing(ledger):
    ledger.log_event({"a": 1}, actor="emptor", event_type="goal_received")
    rows_before = len(ledger._store.get_all_entries())

    with pytest.raises(InvalidEntryError):
        ledger.log_event({"x": float("nan")}, actor="emptor", event_type="goal_received")

    assert len(ledger._store.get_all_entries()) == rows_before


def test_validation_happens_before_any_store_interaction(ledger):
    """An invalid actor/event_type must be rejected before data is even
    canonicalized or the store touched."""
    rows_before = len(ledger._store.get_all_entries())
    with pytest.raises(InvalidEntryError):
        ledger.log_event({"x": 1}, actor="", event_type="goal_received")
    assert len(ledger._store.get_all_entries()) == rows_before


def test_log_event_through_ledger_concurrently_still_verifies(tmp_path):
    """The real section-6 concurrency test: Thread + Barrier racing
    Ledger.log_event (not Store.append directly), then verify_chain()
    proves the resulting chain is still valid."""
    ledger = Ledger(tmp_path / "concurrent.db")
    n_threads = 8
    barrier = threading.Barrier(n_threads)

    def worker(n):
        barrier.wait()
        ledger.log_event({"n": n}, actor="emptor", event_type="goal_received")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = ledger.verify_chain()
    ledger.close()

    assert result.is_valid is True
    assert result.entries_checked == n_threads


# --- Module-level default-instance wrappers (fides.log_event / fides.verify_chain) ---


def test_module_level_wrappers_work_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("FIDES_DB_PATH", str(tmp_path / "default.db"))
    # Reset the lazily-constructed default so this test's env var takes effect,
    # regardless of what earlier tests in the same process may have done.
    ledger_module._default_ledger = None

    entry = fides.log_event({"goal": "gift"}, actor="emptor", event_type="goal_received")
    assert entry.seq == 1

    result = fides.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 1

    # cleanup so later tests in the same process get a fresh default
    ledger_module._default_ledger.close()
    ledger_module._default_ledger = None


def test_get_default_ledger_lazy_init_is_race_safe(tmp_path, monkeypatch):
    """Two threads racing the first-ever call to _get_default_ledger() must
    not construct two Ledger/Store/Lock instances on the same file."""
    monkeypatch.setenv("FIDES_DB_PATH", str(tmp_path / "race_default.db"))
    ledger_module._default_ledger = None

    barrier = threading.Barrier(4)
    results = []

    def worker():
        barrier.wait()
        results.append(ledger_module._get_default_ledger())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    assert all(r is results[0] for r in results), "lazy init produced more than one Ledger instance"

    ledger_module._default_ledger.close()
    ledger_module._default_ledger = None
