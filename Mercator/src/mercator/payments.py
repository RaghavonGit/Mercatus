"""Razorpay payment handling, in either test or live mode.

``create_order`` is the legacy v1 path (order creation only, no capture).
``create_payment_link`` / ``fetch_payment_link`` are the real-money path:
the buyer clicks a hosted link and pays it themselves — no pre-authorized
autopay — and a poller reconciles the outcome later (server.py).

Amount conversion happens in exactly two places: ``rupees_to_paise`` on
the way in and ``_paise_to_rupees`` on the way out. Every other module in
this package deals in integer rupees only.

API shapes verified against Razorpay's current docs:
- Orders (2026-08-25): ``client.order.create({...})`` positional dict.
- Payment Links (2026-08-31, razorpay.com/docs/api/payments/payment-links):
  ``client.payment_link.create({...})`` returns ``id`` + ``short_url``
  (the payable URL — *not* ``url``) + ``status`` in
  {created, issued, partially_paid, expired, cancelled, paid}. No
  idempotency-header support, so a request exception is genuinely
  ambiguous about whether the link was created — hence the
  ``payment_link.all({"reference_id": ...})`` recovery lookup.

The SDK raises on request-level failures which are caught broadly here per
this package's fail-closed rule — but the raw error is *always* surfaced
(stderr + the returned ``detail`` field), never silently swallowed: a bare
``PAYMENT_FAILED`` on a money path is undebuggable.
"""

import sys
import time

import razorpay

# razorpay-python's HTTP layer is ``requests``. A stale keep-alive socket
# (first call after the server has been idle a while) surfaces as
# ``requests.exceptions.ConnectionError`` and means the request almost
# certainly never reached Razorpay — the one exception class it is safe to
# retry. Imported defensively so a missing/renamed dependency degrades to
# "no retry" rather than an ImportError.
try:  # pragma: no cover - requests ships with razorpay
    from requests.exceptions import ConnectionError as _RequestsConnectionError
except Exception:  # pragma: no cover
    _RequestsConnectionError = ()


def _bounded(value: object, limit: int = 200) -> str:
    """Bound and ASCII-clamp an error string — it gets printed to a
    possibly-cp1252 console and logged to the ledger."""
    text = str(value).encode("ascii", "backslashreplace").decode("ascii")
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


class LiveKeyError(RuntimeError):
    pass


_MODE_KEY_PREFIXES = {"test": "rzp_test_", "live": "rzp_live_"}


def assert_mode_matches_key(key_id: str | None, mode: str) -> None:
    expected_prefix = _MODE_KEY_PREFIXES.get(mode)
    if expected_prefix is None:
        raise LiveKeyError(
            f"Refusing to start: unknown RAZORPAY_MODE {mode!r}; must be one of "
            f"{sorted(_MODE_KEY_PREFIXES)}."
        )
    if not isinstance(key_id, str) or not key_id.startswith(expected_prefix):
        raise LiveKeyError(
            f"Refusing to start: RAZORPAY_KEY_ID does not look like a {mode!r}-mode key "
            f"(expected it to start with {expected_prefix!r})."
        )


def rupees_to_paise(amount_inr: int) -> int:
    if not isinstance(amount_inr, int) or isinstance(amount_inr, bool) or amount_inr < 0:
        raise ValueError("amount_inr must be a non-negative int")
    return amount_inr * 100


def _paise_to_rupees(amount_paise) -> int:
    """Razorpay always deals in integer paise; this package deals in
    integer rupees only. Raises on anything that is not an int-like value
    so the caller can fail closed."""
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int):
        raise ValueError("amount_paise must be an int")
    return amount_paise // 100


def make_client(key_id: str, key_secret: str, mode: str) -> razorpay.Client:
    assert_mode_matches_key(key_id, mode)
    return razorpay.Client(auth=(key_id, key_secret))


def create_order(client: razorpay.Client, amount_inr: int, receipt: str) -> dict:
    try:
        order = client.order.create(
            {
                "amount": rupees_to_paise(amount_inr),
                "currency": "INR",
                "receipt": receipt,
            }
        )
    except Exception:
        return {"ok": False, "reason": "PAYMENT_FAILED"}

    order_id = order.get("id") if isinstance(order, dict) else None
    status = order.get("status") if isinstance(order, dict) else None
    if not order_id or not status:
        return {"ok": False, "reason": "PAYMENT_FAILED"}

    return {"ok": True, "order_id": order_id, "amount": amount_inr, "status": status}


# --- Payment Links (real-money path) --------------------------------------

# Razorpay raw statuses that mean "no money has moved yet, keep polling".
_RAW_STATUS_PENDING = {"created", "issued"}


def _normalize_created_link(link, amount_inr: int) -> dict | None:
    """Map a raw Razorpay payment-link object to this package's checkout
    result shape, or ``None`` if it is missing the fields we require."""
    if not isinstance(link, dict):
        return None
    link_id = link.get("id")
    short_url = link.get("short_url")
    if not link_id or not short_url:
        return None
    return {
        "ok": True,
        "payment_link_id": link_id,
        "payment_link_url": short_url,
        "status": "pending",
        "amount": amount_inr,
    }


def _iter_payment_links(listing):
    """``payment_link.all`` returns ``{"payment_links": [...]}``; tolerate a
    bare list too."""
    if isinstance(listing, dict):
        entries = listing.get("payment_links")
        return entries if isinstance(entries, list) else []
    if isinstance(listing, list):
        return listing
    return []


def _recover_created_link(client, cart_id: str, amount_inr: int) -> dict | None:
    """Look up a link by ``reference_id``. Used both to recover from an
    ambiguous ``create`` failure (this endpoint has no idempotency-key
    support, so an exception doesn't tell us whether the link landed) and,
    before any retry, to make sure a retry can't duplicate a link that
    actually did land. Returns ``None`` on lookup failure or no match —
    caller decides what that means."""
    try:
        listing = client.payment_link.all({"reference_id": cart_id})
    except Exception:  # noqa: BLE001 - fail closed; caller records the error
        return None
    for entry in _iter_payment_links(listing):
        if isinstance(entry, dict) and entry.get("reference_id") == cart_id:
            recovered = _normalize_created_link(entry, amount_inr)
            if recovered is not None:
                return recovered
    return None


def create_payment_link(
    client: razorpay.Client,
    amount_inr: int,
    cart_id: str,
    expire_hours: int,
    description: str | None = None,
) -> dict:
    payload = {
        "amount": rupees_to_paise(amount_inr),
        "currency": "INR",
        "reference_id": cart_id,
        "expire_by": int(time.time()) + expire_hours * 3600,
        # Explicit — never rely on the API default. A partial payment must
        # never let a checkout look "paid".
        "accept_partial": False,
    }
    if description is not None:
        payload["description"] = description

    last_error = "no attempt made"
    for attempt in (1, 2):
        try:
            link = client.payment_link.create(payload)
        except Exception as exc:  # noqa: BLE001 - fail closed, but always record why
            last_error = f"create raised {type(exc).__name__}: {exc}"
            # Did it land anyway? reference_id is the only handle we have.
            recovered = _recover_created_link(client, cart_id, amount_inr)
            if recovered is not None:
                return recovered
            # Nothing was created. Retry exactly once, and only for a
            # transport-level failure that means the request never got
            # through (stale keep-alive socket). Any other error is either
            # deterministic (bad payload) or ambiguous (already handled by
            # the recovery lookup above) — retrying would just risk a dup.
            if (
                attempt == 1
                and _RequestsConnectionError
                and isinstance(exc, _RequestsConnectionError)
            ):
                continue
            break

        normalized = _normalize_created_link(link, amount_inr)
        if normalized is not None:
            return normalized

        # The call returned cleanly but the response is unusable. It may
        # have half-succeeded, so don't retry the create — try recovery,
        # then give up.
        last_error = f"create returned an unusable response: {_bounded(link)}"
        recovered = _recover_created_link(client, cart_id, amount_inr)
        if recovered is not None:
            return recovered
        break

    print(
        f"mercator: payment link creation failed for cart {cart_id} - {_bounded(last_error, 400)}",
        file=sys.stderr,
    )
    return {"ok": False, "reason": "PAYMENT_FAILED", "detail": _bounded(last_error)}


def fetch_payment_link(client: razorpay.Client, payment_link_id: str) -> dict:
    """Fetch and normalize a payment link's current state. Fails closed
    toward "still pending, try again next poll" — never toward "paid" or
    "expired" — on any ambiguity."""
    unknown = {
        "payment_link_id": payment_link_id,
        "status": "unknown",
        "amount_paid": 0,
        "amount": 0,
    }
    try:
        link = client.payment_link.fetch(payment_link_id)
    except Exception as exc:  # noqa: BLE001 - fail closed toward "keep polling"
        # The reconciler will retry next tick; still surface it, so a link
        # that is silently un-pollable doesn't just look "pending" forever.
        print(
            f"mercator: could not fetch payment link {payment_link_id} - "
            f"{_bounded(f'{type(exc).__name__}: {exc}')}",
            file=sys.stderr,
        )
        return unknown

    if not isinstance(link, dict):
        return unknown

    raw_status = link.get("status")
    try:
        amount = _paise_to_rupees(link.get("amount"))
        amount_paid = _paise_to_rupees(link.get("amount_paid"))
    except ValueError:
        return unknown

    if raw_status in _RAW_STATUS_PENDING:
        status = "pending"
    elif raw_status == "paid":
        # Defensive: accept_partial=False should make this impossible, but
        # never silently call a short payment "paid".
        status = "paid" if amount_paid == amount else "partially_paid"
    elif raw_status == "partially_paid":
        status = "partially_paid"
    elif raw_status == "expired":
        status = "expired"
    elif raw_status == "cancelled":
        status = "cancelled"
    else:
        return unknown

    return {
        "payment_link_id": payment_link_id,
        "status": status,
        "amount_paid": amount_paid,
        "amount": amount,
    }
