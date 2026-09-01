from unittest.mock import Mock

import pytest

import time

from mercator.payments import (
    LiveKeyError,
    assert_mode_matches_key,
    create_order,
    create_payment_link,
    fetch_payment_link,
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


# --- create_payment_link ---------------------------------------------------


def make_link_client(create=None, create_raises=None, all_return=None, all_raises=None):
    client = Mock()
    if create_raises is not None:
        client.payment_link.create.side_effect = create_raises
    else:
        client.payment_link.create.return_value = create
    if all_raises is not None:
        client.payment_link.all.side_effect = all_raises
    else:
        client.payment_link.all.return_value = all_return
    return client


def test_create_payment_link_success_maps_short_url_to_payment_link_url():
    client = make_link_client(
        create={"id": "plink_abc", "short_url": "https://rzp.io/i/xyz", "status": "created"}
    )
    result = create_payment_link(client, amount_inr=450, cart_id="cart_abc", expire_hours=6)
    assert result == {
        "ok": True,
        "payment_link_id": "plink_abc",
        "payment_link_url": "https://rzp.io/i/xyz",
        "status": "pending",
        "amount": 450,
    }


def test_create_payment_link_payload_amount_currency_reference_and_accept_partial():
    client = make_link_client(create={"id": "plink_abc", "short_url": "https://rzp.io/i/xyz"})
    create_payment_link(client, amount_inr=450, cart_id="cart_abc", expire_hours=6)
    payload = client.payment_link.create.call_args[0][0]
    assert payload["amount"] == 45000
    assert payload["currency"] == "INR"
    assert payload["reference_id"] == "cart_abc"
    assert payload["accept_partial"] is False


def test_create_payment_link_expire_by_is_now_plus_window():
    client = make_link_client(create={"id": "plink_abc", "short_url": "https://rzp.io/i/xyz"})
    before = int(time.time())
    create_payment_link(client, amount_inr=450, cart_id="cart_abc", expire_hours=6)
    after = int(time.time())
    expire_by = client.payment_link.create.call_args[0][0]["expire_by"]
    assert before + 6 * 3600 <= expire_by <= after + 6 * 3600


def test_create_payment_link_accept_partial_always_false_ignores_description_kwarg():
    client = make_link_client(create={"id": "plink_abc", "short_url": "https://rzp.io/i/xyz"})
    create_payment_link(
        client, amount_inr=450, cart_id="cart_abc", expire_hours=6, description="cart cart_abc"
    )
    payload = client.payment_link.create.call_args[0][0]
    assert payload["accept_partial"] is False
    assert payload["description"] == "cart cart_abc"


def test_create_payment_link_create_raises_then_recovery_finds_link_returns_pending():
    client = make_link_client(
        create_raises=RuntimeError("timeout"),
        all_return={
            "payment_links": [
                {"id": "plink_rec", "short_url": "https://rzp.io/i/rec", "reference_id": "cart_abc"}
            ]
        },
    )
    result = create_payment_link(client, amount_inr=450, cart_id="cart_abc", expire_hours=6)
    assert result == {
        "ok": True,
        "payment_link_id": "plink_rec",
        "payment_link_url": "https://rzp.io/i/rec",
        "status": "pending",
        "amount": 450,
    }


def test_create_payment_link_create_raises_recovery_finds_nothing_returns_payment_failed():
    client = make_link_client(
        create_raises=RuntimeError("timeout"),
        all_return={"payment_links": []},
    )
    result = create_payment_link(client, amount_inr=450, cart_id="cart_abc", expire_hours=6)
    assert result == {"ok": False, "reason": "PAYMENT_FAILED"}


def test_create_payment_link_create_raises_recovery_also_raises_returns_payment_failed():
    client = make_link_client(
        create_raises=RuntimeError("timeout"),
        all_raises=RuntimeError("also down"),
    )
    result = create_payment_link(client, amount_inr=450, cart_id="cart_abc", expire_hours=6)
    assert result == {"ok": False, "reason": "PAYMENT_FAILED"}


def test_create_payment_link_recovery_ignores_entries_for_a_different_cart():
    client = make_link_client(
        create_raises=RuntimeError("timeout"),
        all_return={
            "payment_links": [
                {"id": "plink_other", "short_url": "https://rzp.io/i/o", "reference_id": "cart_zzz"}
            ]
        },
    )
    result = create_payment_link(client, amount_inr=450, cart_id="cart_abc", expire_hours=6)
    assert result == {"ok": False, "reason": "PAYMENT_FAILED"}


@pytest.mark.parametrize(
    "malformed",
    [
        {"short_url": "https://rzp.io/i/xyz"},  # missing id
        {"id": "plink_abc"},  # missing short_url
        {"id": "", "short_url": "https://rzp.io/i/xyz"},  # empty id
        None,
    ],
)
def test_create_payment_link_malformed_response_triggers_recovery(malformed):
    client = make_link_client(
        create=malformed,
        all_return={
            "payment_links": [
                {"id": "plink_rec", "short_url": "https://rzp.io/i/rec", "reference_id": "cart_abc"}
            ]
        },
    )
    result = create_payment_link(client, amount_inr=450, cart_id="cart_abc", expire_hours=6)
    assert result["ok"] is True
    assert result["payment_link_id"] == "plink_rec"


# --- fetch_payment_link status normalization ------------------------------


def make_fetch_client(fetch_return=None, fetch_raises=None):
    client = Mock()
    if fetch_raises is not None:
        client.payment_link.fetch.side_effect = fetch_raises
    else:
        client.payment_link.fetch.return_value = fetch_return
    return client


@pytest.mark.parametrize("raw", ["created", "issued"])
def test_fetch_payment_link_created_or_issued_is_pending(raw):
    client = make_fetch_client({"status": raw, "amount": 45000, "amount_paid": 0})
    result = fetch_payment_link(client, "plink_abc")
    assert result == {
        "payment_link_id": "plink_abc",
        "status": "pending",
        "amount_paid": 0,
        "amount": 450,
    }


def test_fetch_payment_link_paid_in_full_is_paid():
    client = make_fetch_client({"status": "paid", "amount": 45000, "amount_paid": 45000})
    result = fetch_payment_link(client, "plink_abc")
    assert result["status"] == "paid"
    assert result["amount_paid"] == 450
    assert result["amount"] == 450


def test_fetch_payment_link_paid_but_short_is_partially_paid():
    client = make_fetch_client({"status": "paid", "amount": 45000, "amount_paid": 20000})
    result = fetch_payment_link(client, "plink_abc")
    assert result["status"] == "partially_paid"


def test_fetch_payment_link_partially_paid_raw_is_partially_paid():
    client = make_fetch_client({"status": "partially_paid", "amount": 45000, "amount_paid": 20000})
    assert fetch_payment_link(client, "plink_abc")["status"] == "partially_paid"


@pytest.mark.parametrize("raw", ["expired", "cancelled"])
def test_fetch_payment_link_terminal_states_pass_through(raw):
    client = make_fetch_client({"status": raw, "amount": 45000, "amount_paid": 0})
    assert fetch_payment_link(client, "plink_abc")["status"] == raw


def test_fetch_payment_link_unknown_raw_status_is_unknown():
    client = make_fetch_client({"status": "something_new", "amount": 45000, "amount_paid": 0})
    assert fetch_payment_link(client, "plink_abc") == {
        "payment_link_id": "plink_abc",
        "status": "unknown",
        "amount_paid": 0,
        "amount": 0,
    }


def test_fetch_payment_link_exception_is_unknown():
    client = make_fetch_client(fetch_raises=RuntimeError("down"))
    assert fetch_payment_link(client, "plink_abc") == {
        "payment_link_id": "plink_abc",
        "status": "unknown",
        "amount_paid": 0,
        "amount": 0,
    }


def test_fetch_payment_link_malformed_amounts_is_unknown():
    client = make_fetch_client({"status": "paid", "amount": None, "amount_paid": "lots"})
    assert fetch_payment_link(client, "plink_abc")["status"] == "unknown"
