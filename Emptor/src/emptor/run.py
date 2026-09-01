from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
from fides import log_event

from emptor.client import ShopConnection
from emptor.config import Config, ConfigError, LLMSettings, load_config
from emptor.decide import DecideError, decide
from emptor.discover import DiscoverError, discover_catalog
from emptor.filters import pre_filter
from emptor.purchase import PendingPurchase, PurchaseError, purchase
from emptor.validate import ValidationResult, validate_picks

_ACTOR = "emptor"


class PreflightError(RuntimeError):
    """The LLM endpoint is not usable. Distinct from DecideError so the
    ``except DecideError`` around the real call never swallows it, and so
    the remediation hint ("start Ollama") differs."""


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


def _report_pending(pending: PendingPurchase) -> None:
    """Print the PENDING line: the checkout went through but the buyer still
    has to pay the link, so no money has moved yet. Same rule as
    _report_success -- once purchase() returned, this is not a failure, and
    no formatting/encoding problem here may turn it into a BLOCKED. Shop-
    supplied text (the link URL and id) is sanitized before printing."""
    try:
        expiry = f", expires in ~{pending.expire_hours}h" if pending.expire_hours else ""
        print(
            f"PENDING: pay INR {pending.total_inr} at {_safe(pending.payment_link_url)} "
            f"(link id {_safe(pending.payment_link_id)}{expiry})"
        )
    except Exception:
        try:
            print(f"PENDING: link id {_safe(pending.payment_link_id)}")
        except Exception:
            pass


def _prompt_operator_approval(goal: str, validated: ValidationResult) -> bool:
    """A second, independent checkpoint on top of the human paying the real
    payment link -- not a replacement for it, and not a money-safety control
    (that stays Mercator's job). Prints the plan and requires the literal
    word ``yes`` typed: not bare Enter, not ``y``. Any other input, or EOF,
    means "not confirmed"."""
    print(f"\nGoal: {_safe(goal)}")
    print("Picks:")
    for item in validated.items:
        print(f"  {item.quantity}x {_safe(item.product['name'])} -- INR {item.line_total_inr}")
    print(f"Total: INR {validated.total_inr}")
    try:
        answer = input("Type 'yes' to confirm this purchase: ")
    except EOFError:
        return False
    return answer == "yes"


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


async def preflight_llm(llm: LLMSettings, *, client: httpx.AsyncClient | None = None) -> None:
    """Fail fast, with the fix in the message, before the pipeline dials the
    shop. Hard-blocks only on an unreachable endpoint. A model that isn't in
    the ``/models`` list is a warning, not a block: Ollama lists only pulled
    blobs, so a miss is almost always an exact-tag mismatch that would
    wrongly stop a working setup -- and decide()'s fallback plus a
    URL-named DecideError still cover the case where it really is missing.
    Logs no Fides event; has no side effects.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=5.0)
    headers = {"Authorization": f"Bearer {llm.api_key}"} if llm.api_key else {}
    try:
        resp = await client.get(f"{llm.base_url}/models", headers=headers)
    except httpx.HTTPError as exc:
        raise PreflightError(
            f"LLM endpoint {llm.base_url} is not reachable ({type(exc).__name__}). "
            "Start it, e.g. 'ollama serve'."
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        raise PreflightError(
            f"LLM endpoint {llm.base_url} returned HTTP {resp.status_code}. "
            "Start it, e.g. 'ollama serve'."
        )

    try:
        listed = {m.get("id") for m in resp.json().get("data", [])}
    except (ValueError, AttributeError, TypeError):
        return  # unparseable list -- don't block on a format quirk
    if listed and llm.model not in listed and llm.fallback_model not in listed:
        print(
            f"WARNING: neither '{llm.model}' nor '{llm.fallback_model}' is listed at "
            f"{llm.base_url}; run 'ollama pull {llm.model}' if the next step fails.",
            file=sys.stderr,
        )


async def run(
    goal: str,
    budget_inr: int,
    config: Config,
    assume_yes: bool = False,
    picks_override: str | None = None,
) -> int:
    purchased_pending_link: PendingPurchase | None = None
    try:
        _safe_log_event(
            {"goal": goal, "budget_inr": budget_inr, "mercator_endpoint": config.mercator_endpoint},
            event_type="goal_received",
        )

        # Fail fast if the LLM is down -- before dialing the shop. Skipped
        # entirely when --picks bypasses the LLM.
        if picks_override is None:
            try:
                await preflight_llm(config.llm)
            except PreflightError as exc:
                print(f"BLOCKED: {_safe(exc)}", file=sys.stderr)
                return 1

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

            if picks_override is not None:
                try:
                    picks = json.loads(picks_override)
                except json.JSONDecodeError:
                    picks = None
                if not isinstance(picks, list):
                    print("BLOCKED: --picks is not a valid JSON array", file=sys.stderr)
                    return 1
                _safe_log_event(
                    {"goal": goal, "picks": picks, "source": "manual-override"},
                    event_type="llm_decision",
                )
            else:
                try:
                    picks = await decide(goal, budget_inr, affordable, config.llm)
                except DecideError as exc:
                    print(f"BLOCKED: LLM decision failed: {_safe(exc)}", file=sys.stderr)
                    return 1
                _safe_log_event(
                    {"goal": goal, "picks": picks, "source": "llm"},
                    event_type="llm_decision",
                )

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

            if not assume_yes and not _prompt_operator_approval(goal, validated):
                _safe_log_event(
                    {
                        "goal": goal,
                        "picks": [
                            {"product_id": item.product["id"], "quantity": item.quantity}
                            for item in validated.items
                        ],
                        "total_inr": validated.total_inr,
                    },
                    event_type="purchase_declined",
                )
                print("DECLINED: purchase not confirmed by operator", file=sys.stderr)
                return 0

            try:
                pending = await purchase(session, validated)
            except PurchaseError as exc:
                print(f"BLOCKED: purchase failed: {_safe(exc)}", file=sys.stderr)
                return 1

            # The checkout request went through. No money has moved yet --
            # the buyer still has to pay the link -- but this is not a
            # failure, and never a BLOCKED past this point (a retry would
            # mint a fresh idempotency key and risk a second purchase once
            # they do pay). Mercator's reconciler logs the real final
            # outcome later, from its own process.
            purchased_pending_link = pending
            _safe_log_event(
                {
                    "payment_link_id": pending.payment_link_id,
                    "total_inr": pending.total_inr,
                    "status": "pending",
                },
                event_type="checkout_result",
            )
            _report_pending(pending)
            return 0
    except Exception as exc:
        if purchased_pending_link is not None:
            # The checkout already went through; never report BLOCKED. The
            # return 0 must not depend on this print succeeding.
            try:
                print(f"PENDING: link id {_safe(purchased_pending_link.payment_link_id)}")
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
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Skip the local operator confirmation prompt (for scripted/test runs). "
            "This bypasses ONLY Emptor's own local checkpoint -- not Mercator's "
            "guardrails, and not the human who still has to pay the payment link."
        ),
    )
    parser.add_argument(
        "--picks",
        default=None,
        help=(
            "Skip the LLM and use these picks verbatim: a JSON array like "
            "'[{\"product_id\": \"sku-1\", \"quantity\": 2}]'. Still validated "
            "and still needs operator confirmation. For testing/demos."
        ),
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"BLOCKED: {_safe(exc)}", file=sys.stderr)
        raise SystemExit(1)

    budget_inr = args.budget if args.budget is not None else config.default_budget_inr

    exit_code = asyncio.run(
        run(args.goal, budget_inr, config, assume_yes=args.yes, picks_override=args.picks)
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
