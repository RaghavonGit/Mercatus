"""Catalog loading and sanitization.

Mercator serves product text to LLM-driven clients, so a malicious or
compromised catalog entry is a prompt-injection vector against whichever
agent is browsing the shop. This module never knowingly serves such a
payload: control/format characters are stripped, fields are length-capped,
and instruction-like text patterns are redacted before a product is
returned from ``load_catalog``. This is defense in depth, not a substitute
for Emptor's own defenses — both sides defend independently (CLAUDE.md
section 6).

Malformed catalog entries (missing/invalid required fields) are skipped
at load time rather than crashing the server.
"""

import json
import re
import unicodedata
from pathlib import Path

MAX_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
MAX_CATEGORY_LENGTH = 100

_ALLOWED_WHITESPACE = {"\n", "\t", " "}

_REDACTION = "[REDACTED]"

# A fixed denylist is inherently bypassable by rephrasing — this stays
# defense-in-depth, not a substitute for Emptor's own defenses (CLAUDE.md
# section 6). The bracket-label character class is deliberately kept
# letters-only for the *generic* directive shape (catches "[SYSTEM: ...]")
# rather than widened to admit digits/hyphens, which would false-positive
# on legitimate spec notation like "[Model X-200: blue]". The separate
# keyword-anchored bracket pattern below catches a digit/hyphen-padded
# label ("[SYSTEM-99: ...]") by matching on the injection keyword itself,
# not on formatting, so it doesn't gain that false-positive risk.
_INJECTION_PATTERNS = [
    re.compile(r"\[\s*[A-Za-z][A-Za-z_ ]{1,30}\s*:.*?\]", re.DOTALL),
    re.compile(
        r"\[[^\]\n]{0,60}\b(?:SYSTEM|ASSISTANT|USER|ADMIN|OVERRIDE|DIRECTIVE|DEVELOPER|PROMPT)\b[^\]\n]{0,60}\]",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\b(?:System|Assistant|User)\s*:", re.IGNORECASE),
    re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior)\s+(?:instructions?|constraints?)", re.IGNORECASE),
    re.compile(
        r"(?:ignore|disregard|regardless\s+of)\s+(?:your\s+|the\s+|any\s+)?(?:spending\s+)?(?:limit|cap|budget)s?",
        re.IGNORECASE,
    ),
    re.compile(r"you\s+must\s+buy", re.IGNORECASE),
    re.compile(r"disregard\s+(?:previous|prior)", re.IGNORECASE),
    re.compile(r"developer\s+mode", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
]


def _strip_control_chars(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if ch in _ALLOWED_WHITESPACE or unicodedata.category(ch) not in ("Cc", "Cf")
    )


def _redact_instruction_patterns(text: str) -> tuple[str, bool]:
    flagged = False
    for pattern in _INJECTION_PATTERNS:
        new_text, count = pattern.subn(_REDACTION, text)
        if count:
            flagged = True
            text = new_text
    return text, flagged


def sanitize_product(product: dict) -> dict:
    cleaned = dict(product)
    flagged = False

    name = _strip_control_chars(str(product.get("name", "")))
    name, name_flagged = _redact_instruction_patterns(name)
    cleaned["name"] = name[:MAX_NAME_LENGTH]
    flagged = flagged or name_flagged

    if "description" in product and product["description"] is not None:
        description = _strip_control_chars(str(product["description"]))
        description, description_flagged = _redact_instruction_patterns(description)
        cleaned["description"] = description[:MAX_DESCRIPTION_LENGTH]
        flagged = flagged or description_flagged

    if "category" in product and product["category"] is not None:
        category = _strip_control_chars(str(product["category"]))
        category, category_flagged = _redact_instruction_patterns(category)
        cleaned["category"] = category[:MAX_CATEGORY_LENGTH]
        flagged = flagged or category_flagged

    cleaned["_flagged"] = flagged
    return cleaned


def _is_valid_entry(entry: dict, seen_ids: set[str]) -> bool:
    if not isinstance(entry, dict):
        return False

    product_id = entry.get("id")
    if not isinstance(product_id, str) or not product_id or product_id in seen_ids:
        return False

    name = entry.get("name")
    if not isinstance(name, str) or not name:
        return False

    price_inr = entry.get("price_inr")
    if not isinstance(price_inr, int) or isinstance(price_inr, bool) or price_inr < 0:
        return False

    in_stock = entry.get("in_stock")
    if not isinstance(in_stock, bool):
        return False

    return True


def load_catalog(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        raw_entries = json.load(f)

    products = []
    seen_ids: set[str] = set()
    for entry in raw_entries:
        if not _is_valid_entry(entry, seen_ids):
            continue
        seen_ids.add(entry["id"])
        products.append(sanitize_product(entry))

    return products
