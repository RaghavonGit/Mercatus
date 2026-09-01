"""Razorpay order creation, in either test or live mode. Order creation
only — v1 stops there, it does not simulate payment capture (CLAUDE.md
section 17).

Amount conversion (integer rupees -> paise, Razorpay's smallest currency
unit) happens in exactly one place: ``rupees_to_paise``, called from
``create_order``. Every other module in this package deals in integer
rupees only.

API shape verified 2026-08-25 against Razorpay's current docs
(razorpay.com/docs/api/orders/create, github.com/razorpay/razorpay-python):
``client.order.create({...})`` takes a positional dict, not a ``data=``
kwarg; required fields are ``amount`` (paise) and ``currency``; the SDK
raises on request-level failures (``razorpay.errors.BadRequestError`` and
others) which are caught broadly here per this package's fail-closed rule.
"""

import razorpay


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
