import pytest

import emptor.config as config_module
from emptor.config import ConfigError, load_config


def test_load_config_reads_env_vars(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NIM_API_KEY", "test-key-123")
    monkeypatch.setenv("MERCATOR_ENDPOINT", "http://example.test:9000")
    monkeypatch.setenv("DEFAULT_BUDGET_INR", "2500")

    config = load_config()

    assert config.nim_api_key == "test-key-123"
    assert config.mercator_endpoint == "http://example.test:9000"
    assert config.default_budget_inr == 2500


def test_load_config_applies_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # load_dotenv() searches from config.py's own file location, not cwd, so
    # chdir alone can't hide a real repo-root .env — stub it out too.
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("NIM_API_KEY", "test-key-123")
    monkeypatch.delenv("MERCATOR_ENDPOINT", raising=False)
    monkeypatch.delenv("DEFAULT_BUDGET_INR", raising=False)

    config = load_config()

    assert config.mercator_endpoint == "http://localhost:8000"
    assert config.default_budget_inr == 1500


def test_load_config_missing_api_key_raises(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)

    with pytest.raises(ConfigError):
        load_config()


def test_load_config_invalid_budget_raises(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NIM_API_KEY", "test-key-123")
    monkeypatch.setenv("DEFAULT_BUDGET_INR", "not-a-number")

    with pytest.raises(ConfigError):
        load_config()


def test_load_config_shop_endpoint_is_driven_by_env_not_hardcoded(monkeypatch, tmp_path):
    # Rule #3 ("no hardcoded shop endpoint") means the target shop is
    # configurable, not that zero URL literals exist in src/ — config.py's
    # http://localhost:8000 fallback (CFG-03) is a documented default, not a
    # hardcoded target. Prove the endpoint actually comes from the env by
    # round-tripping an arbitrary, non-default value through it.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NIM_API_KEY", "test-key-123")
    monkeypatch.setenv("MERCATOR_ENDPOINT", "http://some-other-shop.test:9999")

    config = load_config()

    assert config.mercator_endpoint == "http://some-other-shop.test:9999"


def test_config_repr_does_not_leak_api_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NIM_API_KEY", "super-secret-nim-key")
    monkeypatch.delenv("MERCATOR_ENDPOINT", raising=False)
    monkeypatch.delenv("DEFAULT_BUDGET_INR", raising=False)

    config = load_config()

    assert "super-secret-nim-key" not in repr(config)
    assert "super-secret-nim-key" not in str(config)
