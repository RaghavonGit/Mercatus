import threading

import pytest

from mercator.spend_tracker import SpendTracker


def test_fresh_tracker_sum_is_zero(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    assert tracker.sum_since(window_hours=24) == 0


def test_record_paid_then_sum_includes_it(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.record_paid(450)
    assert tracker.sum_since(window_hours=24) == 450


def test_multiple_records_sum_correctly(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    for amount in (100, 250, 300):
        tracker.record_paid(amount)
    assert tracker.sum_since(window_hours=24) == 650


def test_records_older_than_window_are_excluded(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    now = 1_000_000.0
    # 25h and 48h ago -- outside a 24h window.
    tracker.record_paid(500, ts=now - 25 * 3600)
    tracker.record_paid(700, ts=now - 48 * 3600)
    # 1h ago -- inside.
    tracker.record_paid(200, ts=now - 3600)
    assert tracker.sum_since(window_hours=24, now=now) == 200


def test_record_exactly_at_window_edge_is_excluded(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    now = 1_000_000.0
    tracker.record_paid(300, ts=now - 24 * 3600)  # ts > cutoff is strict
    assert tracker.sum_since(window_hours=24, now=now) == 0


def test_record_paid_amount_zero_is_allowed(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.record_paid(0)
    assert tracker.sum_since(window_hours=24) == 0


@pytest.mark.parametrize("bad_amount", [-1, -500, 4.5, "450", True, None])
def test_record_paid_rejects_bad_amounts(tmp_path, bad_amount):
    tracker = SpendTracker(tmp_path / "spend.db")
    with pytest.raises(ValueError):
        tracker.record_paid(bad_amount)


def test_survives_restart_on_same_path(tmp_path):
    path = tmp_path / "spend.db"
    first = SpendTracker(path)
    first.record_paid(450)
    first.record_paid(300)
    first.close()

    second = SpendTracker(path)
    assert second.sum_since(window_hours=24) == 750


def test_creates_file_in_existing_dir(tmp_path):
    path = tmp_path / "spend.db"
    SpendTracker(path).record_paid(10)
    assert path.exists()


# --- autopay envelope: balance + atomic debit -----------------------------


def test_fresh_tracker_autopay_balance_is_zero(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    assert tracker.autopay_balance() == 0


def test_credit_balance_increases_balance_and_returns_new_total(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    assert tracker.credit_balance(5000) == 5000
    assert tracker.credit_balance(2000) == 7000
    assert tracker.autopay_balance() == 7000


@pytest.mark.parametrize("bad_amount", [-1, -500, 4.5, "450", True, None])
def test_credit_balance_rejects_bad_amounts(tmp_path, bad_amount):
    tracker = SpendTracker(tmp_path / "spend.db")
    with pytest.raises(ValueError):
        tracker.credit_balance(bad_amount)


def test_try_autopay_debit_commits_against_sufficient_balance(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(5000)
    result = tracker.try_autopay_debit("key-1", 300)
    assert result.status == "committed"
    assert result.amount_inr == 300
    assert result.balance_before_inr == 5000
    assert result.balance_after_inr == 4700
    assert tracker.autopay_balance() == 4700


def test_try_autopay_debit_replays_same_key_without_second_debit(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(5000)
    tracker.try_autopay_debit("key-1", 300)
    result = tracker.try_autopay_debit("key-1", 300)
    assert result.status == "replayed"
    assert result.amount_inr == 300
    assert tracker.autopay_balance() == 4700


def test_try_autopay_debit_distinct_keys_each_debit(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(5000)
    tracker.try_autopay_debit("key-1", 300)
    tracker.try_autopay_debit("key-2", 300)
    assert tracker.autopay_balance() == 4400


def test_try_autopay_debit_insufficient_balance_is_a_noop(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(200)
    result = tracker.try_autopay_debit("key-1", 300)
    assert result.status == "insufficient"
    assert result.balance_before_inr == 200
    assert tracker.autopay_balance() == 200
    # a later top-up + retry under a *fresh* key still works
    tracker.credit_balance(500)
    assert tracker.try_autopay_debit("key-2", 300).status == "committed"


def test_try_autopay_debit_exactly_at_balance_commits_to_zero(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(300)
    assert tracker.try_autopay_debit("key-1", 300).status == "committed"
    assert tracker.autopay_balance() == 0


@pytest.mark.parametrize("bad_amount", [0, -1, 4.5, "300", True, None])
def test_try_autopay_debit_rejects_bad_amounts(tmp_path, bad_amount):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(5000)
    with pytest.raises(ValueError):
        tracker.try_autopay_debit("key-1", bad_amount)


@pytest.mark.parametrize("bad_key", ["", "   ", None, 123])
def test_try_autopay_debit_rejects_bad_keys(tmp_path, bad_key):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(5000)
    with pytest.raises(ValueError):
        tracker.try_autopay_debit(bad_key, 300)


def test_sum_since_counts_autopay_debits_alongside_paid_links(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(5000)
    tracker.record_paid(450)
    tracker.try_autopay_debit("key-1", 300)
    assert tracker.sum_since(window_hours=24) == 750


def test_sum_since_excludes_autopay_debits_outside_window(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(5000)
    now = 1_000_000.0
    tracker.try_autopay_debit("old", 500, ts=now - 25 * 3600)
    tracker.try_autopay_debit("new", 200, ts=now - 3600)
    assert tracker.sum_since(window_hours=24, now=now) == 200


def test_autopay_balance_and_debits_survive_restart(tmp_path):
    path = tmp_path / "spend.db"
    first = SpendTracker(path)
    first.credit_balance(5000)
    first.try_autopay_debit("key-1", 300)
    first.close()

    second = SpendTracker(path)
    assert second.autopay_balance() == 4700
    # the durable idempotency claim outlives the restart
    assert second.try_autopay_debit("key-1", 300).status == "replayed"
    assert second.autopay_balance() == 4700


def test_concurrent_debits_distinct_keys_never_overdraw(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(1000)  # covers exactly 3 of the 10 x300 debits

    results = {}
    barrier = threading.Barrier(10)

    def worker(i):
        barrier.wait()
        results[i] = tracker.try_autopay_debit(f"key-{i}", 300).status

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    committed = [i for i, s in results.items() if s == "committed"]
    assert len(committed) == 3
    assert all(results[i] == "insufficient" for i in results if i not in committed)
    assert tracker.autopay_balance() == 100
    assert tracker.autopay_balance() >= 0


def test_concurrent_debits_same_key_debit_exactly_once(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(5000)

    statuses = []
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        s = tracker.try_autopay_debit("shared-key", 300).status
        with lock:
            statuses.append(s)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count("committed") == 1
    assert statuses.count("replayed") == 9
    assert tracker.autopay_balance() == 4700
