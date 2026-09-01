from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


# Local Ollama, OpenAI-compatible endpoint, by default. 127.0.0.1 (not
# "localhost") because on Windows "localhost" resolves ::1 first and Ollama
# binds 127.0.0.1 -- the IPv6 attempt stalls before falling back.
DEFAULT_LLM_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LLM_MODEL = "qwen2.5:7b-instruct"
DEFAULT_LLM_MODEL_FALLBACK = "qwen2.5:3b-instruct"


@dataclass(frozen=True)
class LLMSettings:
    """Where the one LLM call (``decide()``) sends its request. The request
    is plain OpenAI chat-completions shape, so this points at a local Ollama
    server by default but works against any compatible endpoint (NVIDIA NIM,
    OpenAI, LM Studio, ...) by changing ``base_url`` / ``model`` in the
    environment.

    ``api_key`` is optional -- Ollama ignores auth entirely; only a hosted
    provider needs one. Kept out of ``repr`` so it never lands in a log.
    """

    base_url: str
    model: str
    fallback_model: str
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Config:
    llm: LLMSettings
    mercator_endpoint: str
    default_budget_inr: int


def load_config() -> Config:
    load_dotenv()

    base_url = (os.environ.get("LLM_BASE_URL") or DEFAULT_LLM_BASE_URL).rstrip("/")
    model = os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL
    fallback_model = os.environ.get("LLM_MODEL_FALLBACK") or DEFAULT_LLM_MODEL_FALLBACK
    # Fall back to the legacy NIM_API_KEY so an existing .env pointed at
    # NVIDIA NIM keeps working after only LLM_BASE_URL / LLM_MODEL change.
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("NIM_API_KEY") or None

    mercator_endpoint = os.environ.get("MERCATOR_ENDPOINT", "http://localhost:8000")

    raw_budget = os.environ.get("DEFAULT_BUDGET_INR", "1500")
    try:
        default_budget_inr = int(raw_budget)
    except ValueError as exc:
        raise ConfigError(f"DEFAULT_BUDGET_INR must be an integer, got {raw_budget!r}") from exc

    return Config(
        llm=LLMSettings(
            base_url=base_url,
            model=model,
            fallback_model=fallback_model,
            api_key=api_key,
        ),
        mercator_endpoint=mercator_endpoint,
        default_budget_inr=default_budget_inr,
    )
