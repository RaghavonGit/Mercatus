from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    nim_api_key: str = field(repr=False)
    mercator_endpoint: str
    default_budget_inr: int


def load_config() -> Config:
    load_dotenv()

    nim_api_key = os.environ.get("NIM_API_KEY")
    if not nim_api_key:
        raise ConfigError("NIM_API_KEY is not set (check your .env file)")

    mercator_endpoint = os.environ.get("MERCATOR_ENDPOINT", "http://localhost:8000")

    raw_budget = os.environ.get("DEFAULT_BUDGET_INR", "1500")
    try:
        default_budget_inr = int(raw_budget)
    except ValueError as exc:
        raise ConfigError(f"DEFAULT_BUDGET_INR must be an integer, got {raw_budget!r}") from exc

    return Config(
        nim_api_key=nim_api_key,
        mercator_endpoint=mercator_endpoint,
        default_budget_inr=default_budget_inr,
    )
