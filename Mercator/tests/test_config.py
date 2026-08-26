import pytest

from mercator.config import ConfigError, load_config

VALID_ENV = {
    "RAZORPAY_KEY_ID": "rzp_test_abc123",
    "RAZORPAY_KEY_SECRET": "secret123",
    "SPEND_CAP_INR": "1500",
    "ALLOWED_CATEGORIES": "books,toys,stationery",
    "MERCATOR_PORT": "8000",
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
