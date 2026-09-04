from unittest.mock import Mock

import pytest

from mercator.config import AutopayConfig, Config
from mercator.spend_tracker import SpendTracker
from mercator.topup import run_topup

AUTOPAY = AutopayConfig(threshold_inr=800, allowed_categories=["books"], max_balance_inr=5000)


def make_config(autopay=AUTOPAY, expire_hours=None):
    return Config(
        razorpay_key_id="rzp_test_abc",
        razorpay_key_secret="secret",
        spend_cap_inr=1500,
        allowed_categories=["books"],
        port=8000,
        razorpay_mode="test",
        cumulative_spend_cap_inr=None,
        cumulative_spend_window_hours=24,
        payment_link_expire_hours=expire_hours,
        max_pending_payment_links=5,
        autopay=autopay,
    )


def make_client(fetch_statuses=("paid",)):
    """create -> a usable link; fetch -> walks fetch_statuses, repeating the last."""
    client = Mock()
    client.payment_link.create.return_value = {
        "id": "plink_topup_1",
        "short_url": "https://rzp.io/i/plink_topup_1",
        "status": "created",
    }
    client.payment_link.all.return_value = {"payment_links": []}
    seq = list(fetch_statuses)

    def _fetch(_link_id):
        status = seq.pop(0) if len(seq) > 1 else seq[0]
        raw = {"paid": "paid", "pending": "created"}.get(status, status)
        paise = 500000
        return {
            "status": raw,
            "amount": paise,
            "amount_paid": paise if status == "paid" else 0,
        }

    client.payment_link.fetch.side_effect = _fetch
    return client


def test_run_topup_credits_balance_after_link_is_paid(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    client = make_client(fetch_statuses=("pending", "pending", "paid"))
    result = run_topup(
        3000, config=make_config(), razorpay_client=client, spend_tracker=tracker,
        poll_interval_s=0, max_wait_s=100, sleep=lambda _s: None,
    )
    assert result["ok"] is True
    assert result["balance_inr"] == 3000
    assert tracker.autopay_balance() == 3000


def test_run_topup_mints_link_with_topup_reference_id(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    client = make_client()
    run_topup(
        1000, config=make_config(), razorpay_client=client, spend_tracker=tracker,
        poll_interval_s=0, sleep=lambda _s: None,
    )
    _client, args, kwargs = client.payment_link.create.mock_calls[0]
    payload = args[0]
    assert payload["reference_id"].startswith("topup_")
    assert payload["amount"] == 100000  # paise


def test_run_topup_refuses_when_autopay_not_configured(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    result = run_topup(
        1000, config=make_config(autopay=None), razorpay_client=make_client(),
        spend_tracker=tracker, sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert tracker.autopay_balance() == 0


def test_run_topup_refuses_when_ceiling_would_be_exceeded(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(4000)
    client = make_client()
    result = run_topup(
        2000, config=make_config(), razorpay_client=client, spend_tracker=tracker,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert "max" in (result.get("detail", "").lower())
    assert client.payment_link.create.call_count == 0
    assert tracker.autopay_balance() == 4000


def test_run_topup_ceiling_check_is_inclusive_of_current_balance(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    tracker.credit_balance(4000)
    # 4000 + 1000 == 5000 ceiling exactly -> allowed
    result = run_topup(
        1000, config=make_config(), razorpay_client=make_client(), spend_tracker=tracker,
        poll_interval_s=0, sleep=lambda _s: None,
    )
    assert result["ok"] is True
    assert tracker.autopay_balance() == 5000


@pytest.mark.parametrize("bad", [0, -100, 4.5, "1000", None])
def test_run_topup_rejects_bad_amount(tmp_path, bad):
    tracker = SpendTracker(tmp_path / "spend.db")
    result = run_topup(
        bad, config=make_config(), razorpay_client=make_client(), spend_tracker=tracker,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False


def test_run_topup_credits_nothing_when_link_expires(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    client = make_client(fetch_statuses=("pending", "expired"))
    result = run_topup(
        3000, config=make_config(), razorpay_client=client, spend_tracker=tracker,
        poll_interval_s=0, max_wait_s=100, sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert tracker.autopay_balance() == 0


def test_run_topup_credits_nothing_on_poll_timeout(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    client = make_client(fetch_statuses=("pending",))
    result = run_topup(
        3000, config=make_config(), razorpay_client=client, spend_tracker=tracker,
        poll_interval_s=1, max_wait_s=3, sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert tracker.autopay_balance() == 0


def test_run_topup_skip_payment_credits_directly_but_respects_ceiling(tmp_path):
    tracker = SpendTracker(tmp_path / "spend.db")
    client = make_client()
    result = run_topup(
        2000, config=make_config(), razorpay_client=client, spend_tracker=tracker,
        skip_payment=True, sleep=lambda _s: None,
    )
    assert result["ok"] is True
    assert tracker.autopay_balance() == 2000
    assert client.payment_link.create.call_count == 0

    over = run_topup(
        4000, config=make_config(), razorpay_client=client, spend_tracker=tracker,
        skip_payment=True, sleep=lambda _s: None,
    )
    assert over["ok"] is False
    assert tracker.autopay_balance() == 2000
