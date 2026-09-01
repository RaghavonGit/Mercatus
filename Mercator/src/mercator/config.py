"""Spend cap, allowlist, Razorpay keys/mode, port, and the live-money
guardrail fields (cumulative spend cap/window, payment-link expiry and
pending-link limit) — parsed and validated from the environment. An
unparseable required value is a startup failure, never a silent default
(CLAUDE.md section 10).

``load_config`` takes a plain ``dict`` (usually a merge of ``os.environ``
and a loaded ``.env`` file) rather than reading the process environment
directly, so it stays trivially testable.
"""

DEFAULT_PORT = 8000
DEFAULT_CUMULATIVE_SPEND_WINDOW_HOURS = 24
DEFAULT_MAX_PENDING_PAYMENT_LINKS = 5

RAZORPAY_MODES = {"test", "live"}


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
        razorpay_mode: str,
        cumulative_spend_cap_inr: int | None,
        cumulative_spend_window_hours: int,
        payment_link_expire_hours: int | None,
        max_pending_payment_links: int,
    ) -> None:
        self.razorpay_key_id = razorpay_key_id
        self.razorpay_key_secret = razorpay_key_secret
        self.spend_cap_inr = spend_cap_inr
        self.allowed_categories = allowed_categories
        self.port = port
        self.razorpay_mode = razorpay_mode
        self.cumulative_spend_cap_inr = cumulative_spend_cap_inr
        self.cumulative_spend_window_hours = cumulative_spend_window_hours
        self.payment_link_expire_hours = payment_link_expire_hours
        self.max_pending_payment_links = max_pending_payment_links


def _require_str(env: dict, key: str) -> str:
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} is required and must be a non-empty string")
    return value


def _require_one_of(env: dict, key: str, allowed: set[str]) -> str:
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} is required and must be set")
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {sorted(allowed)}, got {value!r}")
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


def _parse_positive_int_or_none(env: dict, key: str) -> int | None:
    value = env.get(key)
    if value is None or not str(value).strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a valid integer, got {value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {parsed}")
    return parsed


def _parse_optional_positive_int(env: dict, key: str, default: int) -> int:
    parsed = _parse_positive_int_or_none(env, key)
    return default if parsed is None else parsed


def _parse_categories(env: dict, key: str) -> list[str]:
    value = env.get(key)
    if not value:
        return []
    return [c.strip() for c in value.split(",") if c.strip()]


def load_config(env: dict) -> Config:
    razorpay_mode = _require_one_of(env, "RAZORPAY_MODE", RAZORPAY_MODES)

    # Optional positive int in test mode (None = no cumulative cap
    # enforced); required once razorpay_mode is "live".
    cumulative_spend_cap_inr = _parse_positive_int_or_none(env, "CUMULATIVE_SPEND_CAP_INR")
    if razorpay_mode == "live" and cumulative_spend_cap_inr is None:
        raise ConfigError("CUMULATIVE_SPEND_CAP_INR is required when RAZORPAY_MODE is live")

    # Optional positive int in test mode (None = Task 3's payments.py picks
    # its own short fallback); required once razorpay_mode is "live".
    payment_link_expire_hours = _parse_positive_int_or_none(env, "PAYMENT_LINK_EXPIRE_HOURS")
    if razorpay_mode == "live" and payment_link_expire_hours is None:
        raise ConfigError("PAYMENT_LINK_EXPIRE_HOURS is required when RAZORPAY_MODE is live")

    return Config(
        razorpay_key_id=_require_str(env, "RAZORPAY_KEY_ID"),
        razorpay_key_secret=_require_str(env, "RAZORPAY_KEY_SECRET"),
        # spend_cap_inr is already unconditionally required today (via
        # _parse_positive_int below), so it's already required in live mode
        # too — no additional live-mode check needed here.
        spend_cap_inr=_parse_positive_int(env, "SPEND_CAP_INR"),
        allowed_categories=_parse_categories(env, "ALLOWED_CATEGORIES"),
        port=_parse_optional_int(env, "MERCATOR_PORT", DEFAULT_PORT),
        razorpay_mode=razorpay_mode,
        cumulative_spend_cap_inr=cumulative_spend_cap_inr,
        cumulative_spend_window_hours=_parse_optional_positive_int(
            env, "CUMULATIVE_SPEND_WINDOW_HOURS", DEFAULT_CUMULATIVE_SPEND_WINDOW_HOURS
        ),
        payment_link_expire_hours=payment_link_expire_hours,
        max_pending_payment_links=_parse_optional_positive_int(
            env, "MAX_PENDING_PAYMENT_LINKS", DEFAULT_MAX_PENDING_PAYMENT_LINKS
        ),
    )
