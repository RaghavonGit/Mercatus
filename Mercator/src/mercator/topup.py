"""``mercator-topup`` -- fund the autopay envelope.

Run once, in advance, by a human -- entirely outside the agent's flow. It
mints a real Razorpay hosted Payment Link for the top-up amount, waits for
the human to pay it, then credits Mercator's durable ``autopay_balance``.
The agent never sees this; ``checkout`` has no way to inflate its own budget.

``run_topup`` is the testable core; ``main`` is the CLI wiring.

Nothing is credited unless the link is actually paid. The
``AUTOPAY_MAX_BALANCE_INR`` ceiling is enforced here (it is the only bound
on funding-time exposure -- top-ups deliberately do not count toward
``CUMULATIVE_SPEND_CAP_INR``).
"""

import argparse
import sys
import time
import uuid

from mercator import payments
from mercator.config import Config, load_config
from mercator.server import DEFAULT_PAYMENT_LINK_EXPIRE_HOURS, DEFAULT_SPEND_TRACKER_PATH, load_env
from mercator.spend_tracker import SpendTracker


def _fail(detail: str) -> dict:
    return {"ok": False, "detail": detail}


def run_topup(
    amount_inr: int,
    *,
    config: Config,
    razorpay_client,
    spend_tracker: SpendTracker,
    skip_payment: bool = False,
    poll_interval_s: float = 5.0,
    max_wait_s: float = 900.0,
    sleep=time.sleep,
    printer=print,
) -> dict:
    if config.autopay is None:
        return _fail("AUTOPAY_ENABLED is not set -- nothing to fund.")
    if isinstance(amount_inr, bool) or not isinstance(amount_inr, int) or amount_inr <= 0:
        return _fail("amount must be a positive integer number of rupees.")

    current = spend_tracker.autopay_balance()
    ceiling = config.autopay.max_balance_inr
    if current + amount_inr > ceiling:
        return _fail(
            f"refused: balance {current} + {amount_inr} would exceed the max "
            f"envelope balance {ceiling} (AUTOPAY_MAX_BALANCE_INR)."
        )

    if skip_payment:
        new_balance = spend_tracker.credit_balance(amount_inr)
        printer(f"credited {amount_inr} (no hosted payment); balance now {new_balance}.")
        return {"ok": True, "balance_inr": new_balance}

    reference = f"topup_{uuid.uuid4().hex}"
    expire_hours = config.payment_link_expire_hours or DEFAULT_PAYMENT_LINK_EXPIRE_HOURS
    link = payments.create_payment_link(razorpay_client, amount_inr, reference, expire_hours)
    if not link.get("ok"):
        return _fail(f"could not create the top-up payment link: {link.get('detail')}")

    link_id = link["payment_link_id"]
    printer(f"Pay this hosted link to fund the envelope with {amount_inr}:")
    printer(f"    {link['payment_link_url']}")
    printer(f"(link {link_id}, expires in ~{expire_hours}h; waiting up to {int(max_wait_s)}s)")

    waited = 0.0
    while waited < max_wait_s:
        status = payments.fetch_payment_link(razorpay_client, link_id).get("status")
        if status == "paid":
            new_balance = spend_tracker.credit_balance(amount_inr)
            printer(f"paid. Envelope balance now {new_balance}.")
            return {"ok": True, "balance_inr": new_balance, "payment_link_id": link_id}
        if status in ("expired", "cancelled"):
            return _fail(f"link {status} before payment -- nothing credited.")
        sleep(poll_interval_s)
        waited += poll_interval_s

    return _fail("timed out waiting for payment -- nothing credited. Re-run to retry.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mercator-topup", description="Fund the autopay envelope.")
    parser.add_argument("amount_inr", type=int, help="rupees to add to the envelope")
    parser.add_argument(
        "--skip-payment",
        action="store_true",
        help="credit directly without a hosted link (assumes payment was made out of band)",
    )
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-wait", type=float, default=900.0)
    args = parser.parse_args()

    env = load_env()
    config = load_config(env)
    razorpay_client = payments.make_client(
        config.razorpay_key_id, config.razorpay_key_secret, config.razorpay_mode
    )
    spend_tracker = SpendTracker(DEFAULT_SPEND_TRACKER_PATH)

    result = run_topup(
        args.amount_inr,
        config=config,
        razorpay_client=razorpay_client,
        spend_tracker=spend_tracker,
        skip_payment=args.skip_payment,
        poll_interval_s=args.poll_interval,
        max_wait_s=args.max_wait,
    )
    spend_tracker.close()
    if not result["ok"]:
        print(f"mercator-topup: {result['detail']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
