"""Forum settings: where the sibling packages live, which ports to use, and
the Razorpay test-mode credentials Forum needs for on-demand payment-status
checks. All paths default to the monorepo layout; every value is
env-overridable via ``Forum/.env``.

Forum is a demo/presentation layer, not a security boundary - it is allowed
to read the other three packages' state directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# .../Mercatus/Forum/src/forum/config.py -> .../Mercatus
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FORUM_ROOT = Path(__file__).resolve().parents[2]


class ForumConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ForumConfig:
    forum_host: str
    forum_port: int
    mercator_dir: Path
    mercator_endpoint: str
    mercator_ledger_db: Path
    mercator_spend_db: Path
    emptor_ledger_db: Path
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_mode: str


def load_forum_config() -> ForumConfig:
    load_dotenv(_FORUM_ROOT / ".env")

    def _path(key: str, default: Path) -> Path:
        raw = os.environ.get(key)
        return Path(raw) if raw else default

    forum_port = int(os.environ.get("FORUM_PORT", "8100"))
    mercator_dir = _path("MERCATOR_DIR", _REPO_ROOT / "Mercator")
    mercator_endpoint = os.environ.get(
        "MERCATOR_ENDPOINT", "http://127.0.0.1:8000/mcp"
    ).rstrip("/")

    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
    mode = os.environ.get("RAZORPAY_MODE", "test").strip()
    if not key_id or not key_secret:
        raise ForumConfigError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET must be set in Forum/.env "
            "(copy the test-mode values from Mercator/.env)."
        )
    if mode != "test":
        raise ForumConfigError(
            f"Forum is a test-mode demo; RAZORPAY_MODE must be 'test', got {mode!r}."
        )

    # Emptor's pipeline logs its Fides events wherever FIDES_DB_PATH points;
    # Forum sets that (in app startup) to this file so the demo has one
    # stable Emptor-side ledger to read.
    emptor_ledger_db = _path("FORUM_LEDGER_DB", _FORUM_ROOT / "forum_ledger.db")

    return ForumConfig(
        forum_host=os.environ.get("FORUM_HOST", "127.0.0.1"),
        forum_port=forum_port,
        mercator_dir=mercator_dir,
        mercator_endpoint=mercator_endpoint,
        mercator_ledger_db=_path("MERCATOR_LEDGER_DB", mercator_dir / "ledger.db"),
        mercator_spend_db=_path("MERCATOR_SPEND_DB", mercator_dir / "spend_tracker.db"),
        emptor_ledger_db=emptor_ledger_db,
        razorpay_key_id=key_id,
        razorpay_key_secret=key_secret,
        razorpay_mode=mode,
    )
