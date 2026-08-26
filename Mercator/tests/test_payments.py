from unittest.mock import Mock

import pytest

from mercator.payments import (
    LiveKeyError,
    assert_test_mode,
    create_order,
    make_client,
    rupees_to_paise,
)


def test_assert_test_mode_accepts_rzp_test_prefix():
    assert_test_mode("rzp_test_abc123")  # does not raise


def test_assert_test_mode_rejects_live_key():
    with pytest.raises(LiveKeyError):
        assert_test_mode("rzp_live_abc123")


@pytest.mark.parametrize("bad_key", ["abc123", "", None, "rzp_test", "RZP_TEST_abc"])
def test_assert_test_mode_rejects_malformed_key(bad_key):
    with pytest.raises(LiveKeyError):
        assert_test_mode(bad_key)


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


def test_make_client_refuses_live_key():
    with pytest.raises(LiveKeyError):
        make_client("rzp_live_abc123", "secret")


def test_make_client_returns_usable_client_for_test_key():
    client = make_client("rzp_test_abc123", "secret")
    assert hasattr(client, "order")
