"""Spend cap, allowlist, Razorpay keys, and port — parsed and validated
from the environment. An unparseable required value is a startup failure,
never a silent default (CLAUDE.md section 10).

``load_config`` takes a plain ``dict`` (usually a merge of ``os.environ``
and a loaded ``.env`` file) rather than reading the process environment
directly, so it stays trivially testable.
"""

DEFAULT_PORT = 8000


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(
        self,
        razorpay_key_id: str,
        razorpay_key_secret: str,
        spend_cap_inr: int,
        allowed_categories: list[str],
        port: int,
    ) -> None:
        self.razorpay_key_id = razorpay_key_id
        self.razorpay_key_secret = razorpay_key_secret
        self.spend_cap_inr = spend_cap_inr
        self.allowed_categories = allowed_categories
        self.port = port


def _require_str(env: dict, key: str) -> str:
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} is required and must be a non-empty string")
    return value


def _parse_positive_int(env: dict, key: str) -> int:
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} is required and must be set")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a valid integer, got {value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {parsed}")
    return parsed


def _parse_optional_int(env: dict, key: str, default: int) -> int:
    value = env.get(key)
    if value is None or not str(value).strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a valid integer, got {value!r}") from exc


def _parse_categories(env: dict, key: str) -> list[str]:
    value = env.get(key)
    if not value:
        return []
    return [c.strip() for c in value.split(",") if c.strip()]


def load_config(env: dict) -> Config:
    return Config(
        razorpay_key_id=_require_str(env, "RAZORPAY_KEY_ID"),
        razorpay_key_secret=_require_str(env, "RAZORPAY_KEY_SECRET"),
        spend_cap_inr=_parse_positive_int(env, "SPEND_CAP_INR"),
        allowed_categories=_parse_categories(env, "ALLOWED_CATEGORIES"),
        port=_parse_optional_int(env, "MERCATOR_PORT", DEFAULT_PORT),
    )
