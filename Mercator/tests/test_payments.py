from unittest.mock import Mock

import pytest

from mercator.payments import (
    LiveKeyError,
    assert_mode_matches_key,
    create_order,
    make_client,
    rupees_to_paise,
)


def test_assert_mode_matches_key_accepts_test_key_in_test_mode():
    assert_mode_matches_key("rzp_test_abc123", "test")  # does not raise


def test_assert_mode_matches_key_accepts_live_key_in_live_mode():
    assert_mode_matches_key("rzp_live_abc123", "live")  # does not raise


def test_assert_mode_matches_key_rejects_test_key_in_live_mode():
    with pytest.raises(LiveKeyError):
        assert_mode_matches_key("rzp_test_abc123", "live")


def test_assert_mode_matches_key_rejects_live_key_in_test_mode():
    with pytest.raises(LiveKeyError):
        assert_mode_matches_key("rzp_live_abc123", "test")


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("bad_key", ["abc123", "", None, "rzp_test", "RZP_TEST_abc", "RZP_LIVE_abc"])
def test_assert_mode_matches_key_rejects_malformed_key_regardless_of_mode(bad_key, mode):
    with pytest.raises(LiveKeyError):
        assert_mode_matches_key(bad_key, mode)


@pytest.mark.parametrize("bad_mode", ["Test", "LIVE", "sandbox", "", None, "prod"])
def test_assert_mode_matches_key_rejects_invalid_mode_string(bad_mode):
    with pytest.raises(LiveKeyError):
        assert_mode_matches_key("rzp_test_abc123", bad_mode)


def test_rupees_to_paise_conversion():
    assert rupees_to_paise(450) == 45000


def test_rupees_to_paise_zero():
    assert rupees_to_paise(0) == 0


def test_rupees_to_paise_rejects_negative():
    with pytest.raises(ValueError):
        rupees_to_paise(-1)


@pytest.mark.parametrize("bad_amount", [450.0, True, "450"])
def test_rupees_to_paise_rejects_non_int(bad_amount):
    with pytest.raises(ValueError):
        rupees_to_paise(bad_amount)


def make_fake_client(order_response=None, raises=None):
    client = Mock()
    if raises is not None:
        client.order.create.side_effect = raises
    else:
        client.order.create.return_value = order_response
    return client


def test_create_order_success_returns_order_id_and_status():
    client = make_fake_client(order_response={"id": "order_xyz", "status": "created", "amount": 45000})
    result = create_order(client, amount_inr=450, receipt="cart_abc123")
    assert result == {"ok": True, "order_id": "order_xyz", "amount": 450, "status": "created"}


def test_create_order_uses_paise_amount():
    client = make_fake_client(order_response={"id": "order_xyz", "status": "created"})
    create_order(client, amount_inr=450, receipt="cart_abc123")
    called_with = client.order.create.call_args[0][0]
    assert called_with["amount"] == 45000
    assert called_with["currency"] == "INR"


def test_create_order_failure_returns_payment_failed():
    client = make_fake_client(raises=RuntimeError("bad request"))
    result = create_order(client, amount_inr=450, receipt="cart_abc123")
    assert result == {"ok": False, "reason": "PAYMENT_FAILED"}


def test_create_order_malformed_response_missing_id_returns_payment_failed():
    client = make_fake_client(order_response={"status": "created"})
    result = create_order(client, amount_inr=450, receipt="cart_abc123")
    assert result == {"ok": False, "reason": "PAYMENT_FAILED"}


def test_create_order_malformed_response_missing_status_returns_payment_failed():
    client = make_fake_client(order_response={"id": "order_xyz"})
    result = create_order(client, amount_inr=450, receipt="cart_abc123")
    assert result == {"ok": False, "reason": "PAYMENT_FAILED"}


# --- make_client: full mode x key-prefix matrix -----------------------------


def test_make_client_test_key_with_mode_test_succeeds():
    client = make_client("rzp_test_abc123", "secret", "test")
    assert hasattr(client, "order")


def test_make_client_live_key_with_mode_live_succeeds():
    # razorpay.Client(auth=...) does not make any network call on
    # construction, so this is safe to exercise with a live-shaped key.
    client = make_client("rzp_live_abc123", "secret", "live")
    assert hasattr(client, "order")


def test_make_client_test_key_with_mode_live_fails():
    with pytest.raises(LiveKeyError):
        make_client("rzp_test_abc123", "secret", "live")


def test_make_client_live_key_with_mode_test_fails():
    with pytest.raises(LiveKeyError):
        make_client("rzp_live_abc123", "secret", "test")


@pytest.mark.parametrize("mode", ["test", "live"])
def test_make_client_malformed_key_fails_regardless_of_mode(mode):
    with pytest.raises(LiveKeyError):
        make_client("not_a_real_key", "secret", mode)


def test_make_client_rejects_invalid_mode_string():
    with pytest.raises(LiveKeyError):
        make_client("rzp_test_abc123", "secret", "sandbox")
