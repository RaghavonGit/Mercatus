import pytest

from mercator.config import AutopayConfig, ConfigError, load_config

VALID_ENV = {
    "RAZORPAY_KEY_ID": "rzp_test_abc123",
    "RAZORPAY_KEY_SECRET": "secret123",
    "SPEND_CAP_INR": "1500",
    "ALLOWED_CATEGORIES": "books,toys,stationery",
    "MERCATOR_PORT": "8000",
    "RAZORPAY_MODE": "test",
}

# Overrides that put load_config into live mode with its two live-only
# required fields supplied, for tests that need a valid live config.
LIVE_OVERRIDES = {
    "RAZORPAY_MODE": "live",
    "CUMULATIVE_SPEND_CAP_INR": "5000",
    "PAYMENT_LINK_EXPIRE_HOURS": "6",
}


def env(**overrides):
    e = dict(VALID_ENV)
    e.update(overrides)
    for key, value in list(e.items()):
        if value is None:
            del e[key]
    return e


def test_load_config_valid_env_returns_config():
    config = load_config(env())
    assert config.razorpay_key_id == "rzp_test_abc123"
    assert config.razorpay_key_secret == "secret123"
    assert config.spend_cap_inr == 1500
    assert config.allowed_categories == ["books", "toys", "stationery"]
    assert config.port == 8000
    assert config.razorpay_mode == "test"
    assert config.cumulative_spend_cap_inr is None
    assert config.cumulative_spend_window_hours == 24
    assert config.payment_link_expire_hours is None
    assert config.max_pending_payment_links == 5


def test_load_config_missing_razorpay_key_id_raises():
    with pytest.raises(ConfigError):
        load_config(env(RAZORPAY_KEY_ID=None))


def test_load_config_missing_razorpay_key_secret_raises():
    with pytest.raises(ConfigError):
        load_config(env(RAZORPAY_KEY_SECRET=None))


def test_load_config_missing_spend_cap_raises():
    with pytest.raises(ConfigError):
        load_config(env(SPEND_CAP_INR=None))


def test_load_config_unparseable_spend_cap_raises():
    with pytest.raises(ConfigError):
        load_config(env(SPEND_CAP_INR="not-a-number"))


def test_load_config_negative_spend_cap_raises():
    with pytest.raises(ConfigError):
        load_config(env(SPEND_CAP_INR="-5"))


def test_load_config_zero_spend_cap_raises():
    with pytest.raises(ConfigError):
        load_config(env(SPEND_CAP_INR="0"))


def test_load_config_missing_allowed_categories_returns_empty_list():
    config = load_config(env(ALLOWED_CATEGORIES=None))
    assert config.allowed_categories == []


def test_load_config_empty_string_allowed_categories_returns_empty_list():
    config = load_config(env(ALLOWED_CATEGORIES=""))
    assert config.allowed_categories == []


def test_load_config_allowed_categories_strips_whitespace_and_drops_empties():
    config = load_config(env(ALLOWED_CATEGORIES=" books, toys ,,stationery "))
    assert config.allowed_categories == ["books", "toys", "stationery"]


def test_load_config_port_defaults_to_8000_when_unset():
    config = load_config(env(MERCATOR_PORT=None))
    assert config.port == 8000


def test_load_config_port_parses_when_set():
    config = load_config(env(MERCATOR_PORT="9000"))
    assert config.port == 9000


def test_load_config_port_unparseable_raises():
    with pytest.raises(ConfigError):
        load_config(env(MERCATOR_PORT="not-a-port"))


# --- razorpay_mode ---------------------------------------------------------


def test_load_config_missing_razorpay_mode_raises():
    with pytest.raises(ConfigError):
        load_config(env(RAZORPAY_MODE=None))


def test_load_config_empty_razorpay_mode_raises():
    with pytest.raises(ConfigError):
        load_config(env(RAZORPAY_MODE=""))


@pytest.mark.parametrize("bad_mode", ["Test", "LIVE", "sandbox", "test ", " live", "prod"])
def test_load_config_invalid_razorpay_mode_raises(bad_mode):
    with pytest.raises(ConfigError):
        load_config(env(RAZORPAY_MODE=bad_mode))


def test_load_config_razorpay_mode_test_accepted():
    config = load_config(env(RAZORPAY_MODE="test"))
    assert config.razorpay_mode == "test"


def test_load_config_razorpay_mode_live_accepted():
    config = load_config(env(**LIVE_OVERRIDES))
    assert config.razorpay_mode == "live"


# --- cumulative_spend_cap_inr ----------------------------------------------


def test_load_config_cumulative_spend_cap_defaults_to_none_in_test_mode():
    config = load_config(env())
    assert config.cumulative_spend_cap_inr is None


def test_load_config_cumulative_spend_cap_parses_when_set():
    config = load_config(env(CUMULATIVE_SPEND_CAP_INR="3000"))
    assert config.cumulative_spend_cap_inr == 3000


def test_load_config_cumulative_spend_cap_unparseable_raises():
    with pytest.raises(ConfigError):
        load_config(env(CUMULATIVE_SPEND_CAP_INR="not-a-number"))


def test_load_config_cumulative_spend_cap_negative_raises():
    with pytest.raises(ConfigError):
        load_config(env(CUMULATIVE_SPEND_CAP_INR="-5"))


def test_load_config_cumulative_spend_cap_zero_raises():
    with pytest.raises(ConfigError):
        load_config(env(CUMULATIVE_SPEND_CAP_INR="0"))


def test_load_config_live_mode_requires_cumulative_spend_cap():
    with pytest.raises(ConfigError):
        load_config(env(RAZORPAY_MODE="live", PAYMENT_LINK_EXPIRE_HOURS="6"))


def test_load_config_live_mode_blank_cumulative_spend_cap_raises():
    with pytest.raises(ConfigError):
        load_config(
            env(RAZORPAY_MODE="live", PAYMENT_LINK_EXPIRE_HOURS="6", CUMULATIVE_SPEND_CAP_INR="")
        )


# --- cumulative_spend_window_hours ------------------------------------------


def test_load_config_cumulative_spend_window_hours_defaults_to_24():
    config = load_config(env())
    assert config.cumulative_spend_window_hours == 24


def test_load_config_cumulative_spend_window_hours_parses_when_set():
    config = load_config(env(CUMULATIVE_SPEND_WINDOW_HOURS="48"))
    assert config.cumulative_spend_window_hours == 48


def test_load_config_cumulative_spend_window_hours_unparseable_raises():
    with pytest.raises(ConfigError):
        load_config(env(CUMULATIVE_SPEND_WINDOW_HOURS="not-a-number"))


def test_load_config_cumulative_spend_window_hours_zero_raises():
    with pytest.raises(ConfigError):
        load_config(env(CUMULATIVE_SPEND_WINDOW_HOURS="0"))


def test_load_config_cumulative_spend_window_hours_negative_raises():
    with pytest.raises(ConfigError):
        load_config(env(CUMULATIVE_SPEND_WINDOW_HOURS="-1"))


def test_load_config_cumulative_spend_window_hours_defaults_to_24_in_live_mode():
    config = load_config(env(**LIVE_OVERRIDES))
    assert config.cumulative_spend_window_hours == 24


# --- payment_link_expire_hours ----------------------------------------------


def test_load_config_payment_link_expire_hours_defaults_to_none_in_test_mode():
    config = load_config(env())
    assert config.payment_link_expire_hours is None


def test_load_config_payment_link_expire_hours_parses_when_set():
    config = load_config(env(PAYMENT_LINK_EXPIRE_HOURS="12"))
    assert config.payment_link_expire_hours == 12


def test_load_config_payment_link_expire_hours_unparseable_raises():
    with pytest.raises(ConfigError):
        load_config(env(PAYMENT_LINK_EXPIRE_HOURS="not-a-number"))


def test_load_config_payment_link_expire_hours_zero_raises():
    with pytest.raises(ConfigError):
        load_config(env(PAYMENT_LINK_EXPIRE_HOURS="0"))


def test_load_config_payment_link_expire_hours_negative_raises():
    with pytest.raises(ConfigError):
        load_config(env(PAYMENT_LINK_EXPIRE_HOURS="-3"))


def test_load_config_live_mode_requires_payment_link_expire_hours():
    with pytest.raises(ConfigError):
        load_config(env(RAZORPAY_MODE="live", CUMULATIVE_SPEND_CAP_INR="5000"))


def test_load_config_live_mode_blank_payment_link_expire_hours_raises():
    with pytest.raises(ConfigError):
        load_config(
            env(RAZORPAY_MODE="live", CUMULATIVE_SPEND_CAP_INR="5000", PAYMENT_LINK_EXPIRE_HOURS="")
        )


def test_load_config_live_mode_with_both_required_fields_succeeds():
    config = load_config(env(**LIVE_OVERRIDES))
    assert config.razorpay_mode == "live"
    assert config.cumulative_spend_cap_inr == 5000
    assert config.payment_link_expire_hours == 6


# --- max_pending_payment_links -----------------------------------------------


def test_load_config_max_pending_payment_links_defaults_to_5():
    config = load_config(env())
    assert config.max_pending_payment_links == 5


def test_load_config_max_pending_payment_links_parses_when_set():
    config = load_config(env(MAX_PENDING_PAYMENT_LINKS="10"))
    assert config.max_pending_payment_links == 10


def test_load_config_max_pending_payment_links_unparseable_raises():
    with pytest.raises(ConfigError):
        load_config(env(MAX_PENDING_PAYMENT_LINKS="not-a-number"))


def test_load_config_max_pending_payment_links_zero_raises():
    with pytest.raises(ConfigError):
        load_config(env(MAX_PENDING_PAYMENT_LINKS="0"))


def test_load_config_max_pending_payment_links_negative_raises():
    with pytest.raises(ConfigError):
        load_config(env(MAX_PENDING_PAYMENT_LINKS="-1"))


def test_load_config_max_pending_payment_links_defaults_to_5_in_live_mode():
    config = load_config(env(**LIVE_OVERRIDES))
    assert config.max_pending_payment_links == 5


# --- autopay (the tiered-autonomy envelope) --------------------------------

# A valid AUTOPAY_ENABLED=true env: threshold strictly under SPEND_CAP_INR
# (1500), categories a subset of ALLOWED_CATEGORIES (books,toys,stationery).
AUTOPAY_ON = {
    "AUTOPAY_ENABLED": "true",
    "AUTOPAY_THRESHOLD_INR": "800",
    "AUTOPAY_ALLOWED_CATEGORIES": "books,stationery",
    "AUTOPAY_MAX_BALANCE_INR": "5000",
}


def test_load_config_autopay_unset_is_none():
    assert load_config(env()).autopay is None


def test_load_config_autopay_disabled_is_none():
    assert load_config(env(AUTOPAY_ENABLED="false")).autopay is None


def test_load_config_autopay_enabled_returns_autopay_config():
    config = load_config(env(**AUTOPAY_ON))
    assert isinstance(config.autopay, AutopayConfig)
    assert config.autopay.threshold_inr == 800
    assert config.autopay.allowed_categories == ["books", "stationery"]
    assert config.autopay.max_balance_inr == 5000


@pytest.mark.parametrize("bad", ["yes", "1", "True", "FALSE", "on", "enabled"])
def test_load_config_autopay_enabled_unparseable_raises(bad):
    with pytest.raises(ConfigError):
        load_config(env(AUTOPAY_ENABLED=bad))


def test_load_config_autopay_enabled_missing_threshold_raises():
    e = env(**AUTOPAY_ON)
    del e["AUTOPAY_THRESHOLD_INR"]
    with pytest.raises(ConfigError):
        load_config(e)


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-100"])
def test_load_config_autopay_threshold_invalid_raises(bad):
    with pytest.raises(ConfigError):
        load_config(env(**{**AUTOPAY_ON, "AUTOPAY_THRESHOLD_INR": bad}))


def test_load_config_autopay_threshold_equal_to_spend_cap_raises():
    with pytest.raises(ConfigError):
        load_config(env(**{**AUTOPAY_ON, "AUTOPAY_THRESHOLD_INR": "1500"}))


def test_load_config_autopay_threshold_above_spend_cap_raises():
    with pytest.raises(ConfigError):
        load_config(env(**{**AUTOPAY_ON, "AUTOPAY_THRESHOLD_INR": "2000"}))


def test_load_config_autopay_enabled_missing_categories_raises():
    e = env(**AUTOPAY_ON)
    del e["AUTOPAY_ALLOWED_CATEGORIES"]
    with pytest.raises(ConfigError):
        load_config(e)


def test_load_config_autopay_empty_categories_raises():
    with pytest.raises(ConfigError):
        load_config(env(**{**AUTOPAY_ON, "AUTOPAY_ALLOWED_CATEGORIES": ""}))


def test_load_config_autopay_categories_not_a_subset_raises():
    with pytest.raises(ConfigError):
        load_config(env(**{**AUTOPAY_ON, "AUTOPAY_ALLOWED_CATEGORIES": "books,electronics"}))


def test_load_config_autopay_categories_empty_main_allowlist_raises():
    with pytest.raises(ConfigError):
        load_config(env(**{**AUTOPAY_ON, "ALLOWED_CATEGORIES": ""}))


def test_load_config_autopay_categories_strips_whitespace():
    config = load_config(env(**{**AUTOPAY_ON, "AUTOPAY_ALLOWED_CATEGORIES": " books , toys ,,"}))
    assert config.autopay.allowed_categories == ["books", "toys"]


def test_load_config_autopay_enabled_missing_max_balance_raises():
    e = env(**AUTOPAY_ON)
    del e["AUTOPAY_MAX_BALANCE_INR"]
    with pytest.raises(ConfigError):
        load_config(e)


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-1"])
def test_load_config_autopay_max_balance_invalid_raises(bad):
    with pytest.raises(ConfigError):
        load_config(env(**{**AUTOPAY_ON, "AUTOPAY_MAX_BALANCE_INR": bad}))


def test_load_config_autopay_enabled_in_live_mode():
    config = load_config(env(**LIVE_OVERRIDES, **AUTOPAY_ON))
    assert isinstance(config.autopay, AutopayConfig)
