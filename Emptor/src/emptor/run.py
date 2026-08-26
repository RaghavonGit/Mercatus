from __future__ import annotations

import argparse
import asyncio
import sys

from fides import log_event

from emptor.client import ShopConnection
from emptor.config import Config, ConfigError, load_config
from emptor.decide import DecideError, decide
from emptor.discover import DiscoverError, discover_catalog
from emptor.filters import pre_filter
from emptor.purchase import PurchaseError, purchase
from emptor.validate import ValidationResult, validate_picks

_ACTOR = "emptor"


def _safe(value: object, limit: int = 200) -> str:
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


def _describe(exc: BaseException) -> str:
    """Unwrap a (possibly nested) ExceptionGroup to its first real cause.

    mcp's streamable_http_client runs inside an anyio TaskGroup, so a
    connection failure (e.g. no shop server listening) surfaces as an
    ExceptionGroup whose default message - "unhandled errors in a TaskGroup
    (1 sub-exception)" - says nothing a human can act on. Only used for the
    final catch-all below; every other BLOCKED message already comes from a
    specific, already-readable *Error type.
    """
    current: BaseException = exc
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    return f"{type(current).__name__}: {current}"


def _report_success(validated: ValidationResult, order_id: str) -> None:
    """Print the success line.

    Once purchase() has returned, the run has succeeded. Nothing here may turn
    that into a reported failure, so any formatting/encoding problem falls back
    to a minimal ASCII-safe message. Untrusted, shop-supplied text (product
    names, the order id) is sanitized before printing.
    """
    try:
        names = ", ".join(
            f"{item.quantity}x {_safe(item.product['name'])}" for item in validated.items
        )
        print(f"SUCCESS: bought {names} for INR {validated.total_inr} (order {_safe(order_id)})")
    except Exception:
        # Masking is the intent here, not a risk: the requirement is to report
        # success no matter what once money has moved.
        try:
            print(f"SUCCESS: order {_safe(order_id)}")
        except Exception:
            pass


def _safe_log_event(data: dict, *, event_type: str) -> None:
    """Fail-open wrapper around fides.log_event, mirroring Mercator's own
    _safe_log/_safe_log_checkout_result fail-open convention. A ledger
    write failure must never abort the pipeline or -- worse -- turn an
    already-completed purchase into a false BLOCKED report, so this
    swallows everything, including bugs beyond FidesError itself.
    """
    try:
        log_event(data, actor=_ACTOR, event_type=event_type)
    except Exception as exc:
        try:
            print(f"WARNING: ledger write failed ({event_type}): {_safe(exc)}", file=sys.stderr)
        except Exception:
            pass


async def run(goal: str, budget_inr: int, config: Config) -> int:
    purchased_order_id: str | None = None
    try:
        _safe_log_event(
            {"goal": goal, "budget_inr": budget_inr, "mercator_endpoint": config.mercator_endpoint},
            event_type="goal_received",
        )
        async with ShopConnection(config.mercator_endpoint) as session:
            try:
                catalog = await discover_catalog(session)
            except DiscoverError as exc:
                print(f"BLOCKED: could not read shop catalog: {_safe(exc)}", file=sys.stderr)
                return 1

            affordable = pre_filter(catalog, budget_inr)
            _safe_log_event(
                {"catalog_size": len(catalog), "affordable_count": len(affordable)},
                event_type="catalog_retrieved",
            )
            if not affordable:
                print("BLOCKED: no in-stock products within budget", file=sys.stderr)
                return 1

            try:
                picks = await decide(goal, budget_inr, affordable, config.nim_api_key)
            except DecideError as exc:
                print(f"BLOCKED: LLM decision failed: {_safe(exc)}", file=sys.stderr)
                return 1

            _safe_log_event({"goal": goal, "picks": picks}, event_type="llm_decision")

            validated = validate_picks(picks, affordable, budget_inr)
            _safe_log_event(
                {
                    "ok": validated.ok,
                    "reason": validated.reason,
                    "items": [
                        {
                            "product_id": item.product["id"],
                            "quantity": item.quantity,
                            "line_total_inr": item.line_total_inr,
                        }
                        for item in validated.items
                    ],
                    "total_inr": validated.total_inr,
                    "budget_inr": budget_inr,
                },
                event_type="validation_result",
            )
            if not validated.ok:
                print(f"BLOCKED: {_safe(validated.reason)}", file=sys.stderr)
                return 1

            try:
                order_id = await purchase(session, validated)
            except PurchaseError as exc:
                print(f"BLOCKED: purchase failed: {_safe(exc)}", file=sys.stderr)
                return 1

            # Money has moved. From here on the run has succeeded, no matter
            # what reporting or connection teardown does.
            purchased_order_id = order_id
            _safe_log_event(
                {"order_id": order_id, "total_inr": validated.total_inr},
                event_type="checkout_result",
            )
            _report_success(validated, order_id)
            return 0
    except Exception as exc:
        if purchased_order_id is not None:
            # The purchase completed; never report BLOCKED (a retry would
            # mint a fresh idempotency key and double-charge). The return 0
            # must not depend on this print succeeding.
            try:
                print(f"SUCCESS: order {_safe(purchased_order_id)}")
            except Exception:
                pass
            return 0
        print(f"BLOCKED: unexpected error: {_safe(_describe(exc))}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="emptor", description="Autonomous shopper agent")
    parser.add_argument("goal", help="Plain-English shopping goal")
    parser.add_argument(
        "--budget", type=int, default=None, help="Budget in INR (overrides DEFAULT_BUDGET_INR)"
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"BLOCKED: {_safe(exc)}", file=sys.stderr)
        raise SystemExit(1)

    budget_inr = args.budget if args.budget is not None else config.default_budget_inr

    exit_code = asyncio.run(run(args.goal, budget_inr, config))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
