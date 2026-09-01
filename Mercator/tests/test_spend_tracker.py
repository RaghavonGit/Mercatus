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
