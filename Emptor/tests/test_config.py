import pytest

import emptor.config as config_module
from emptor.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    # Emptor/.env exists in the worktree and load_dotenv() reads it from
    # config.py's own location (not cwd), so monkeypatch.chdir can't hide
    # it. Stub it out for every config test so the environment is exactly
    # what each test sets.
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: False)
    for key in (
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_MODEL_FALLBACK",
        "LLM_API_KEY",
        "NIM_API_KEY",
        "MERCATOR_ENDPOINT",
        "DEFAULT_BUDGET_INR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_load_config_reads_env_vars(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test:9000/v1")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.setenv("LLM_MODEL_FALLBACK", "some-small-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key-123")
    monkeypatch.setenv("MERCATOR_ENDPOINT", "http://example.test:9000")
    monkeypatch.setenv("DEFAULT_BUDGET_INR", "2500")

    config = load_config()

    assert config.llm.base_url == "http://llm.test:9000/v1"
    assert config.llm.model == "some-model"
    assert config.llm.fallback_model == "some-small-model"
    assert config.llm.api_key == "test-key-123"
    assert config.mercator_endpoint == "http://example.test:9000"
    assert config.default_budget_inr == 2500


def test_load_config_applies_defaults():
    config = load_config()

    assert config.llm.base_url == "http://127.0.0.1:11434/v1"
    assert config.llm.model == "qwen2.5:7b-instruct"
    assert config.llm.fallback_model == "qwen2.5:3b-instruct"
    assert config.llm.api_key is None
    assert config.mercator_endpoint == "http://localhost:8000"
    assert config.default_budget_inr == 1500


def test_load_config_no_llm_key_is_ok():
    # A local model needs no key -- a missing key is not an error.
    config = load_config()
    assert config.llm.api_key is None


def test_load_config_strips_trailing_slash_from_base_url(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test:9000/v1/")
    assert load_config().llm.base_url == "http://llm.test:9000/v1"


def test_load_config_nim_api_key_is_used_as_llm_key_fallback(monkeypatch):
    # Back-compat: an existing .env pointed at NVIDIA NIM keeps working
    # after only LLM_BASE_URL / LLM_MODEL change.
    monkeypatch.setenv("NIM_API_KEY", "legacy-nim-key")
    assert load_config().llm.api_key == "legacy-nim-key"


def test_load_config_llm_api_key_wins_over_nim_api_key(monkeypatch):
    monkeypatch.setenv("NIM_API_KEY", "legacy-nim-key")
    monkeypatch.setenv("LLM_API_KEY", "new-llm-key")
    assert load_config().llm.api_key == "new-llm-key"


def test_load_config_invalid_budget_raises(monkeypatch):
    monkeypatch.setenv("DEFAULT_BUDGET_INR", "not-a-number")
    with pytest.raises(ConfigError):
        load_config()


def test_load_config_shop_endpoint_is_driven_by_env_not_hardcoded(monkeypatch):
    # Rule #3 ("no hardcoded shop endpoint"): prove the endpoint comes from
    # the env by round-tripping an arbitrary non-default value.
    monkeypatch.setenv("MERCATOR_ENDPOINT", "http://some-other-shop.test:9999")
    assert load_config().mercator_endpoint == "http://some-other-shop.test:9999"


def test_config_repr_does_not_leak_api_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "super-secret-llm-key")

    config = load_config()

    assert "super-secret-llm-key" not in repr(config)
    assert "super-secret-llm-key" not in str(config)
