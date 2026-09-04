"""The demo pipeline: the same steps Emptor's ``run()`` performs, but
yielding a structured event per stage so the browser can animate them, and
writing the same five Fides events (actor ``emptor``) so the ledger panel
is real.

It deliberately reuses Emptor's building blocks
(``discover_catalog`` / ``pre_filter`` / ``decide`` / ``validate_picks`` /
``purchase``) rather than calling ``run()`` - ``run()`` prints and returns
an exit code, with no structured channel, and it is the security-reviewed
orchestrator we don't want to fork. Forum is a presentation layer; the
money-safety boundary stays entirely in Mercator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from emptor.client import ShopConnection
from emptor.config import LLMSettings
from emptor.decide import DecideError, decide
from emptor.discover import DiscoverError, discover_catalog
from emptor.filters import pre_filter
from emptor.purchase import PurchaseError, SettledPurchase, purchase
from emptor.validate import (
    MAX_DISTINCT_ITEMS,
    MAX_QTY_PER_ITEM,
    validate_picks,
)
from fides import log_event

_ACTOR = "emptor"


def _safe_log(data: dict, event_type: str) -> None:
    try:
        log_event(data, actor=_ACTOR, event_type=event_type)
    except Exception:  # noqa: BLE001 - a ledger hiccup must not break the demo
        pass


def _product_view(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "price_inr": p.get("price_inr"),
        "in_stock": p.get("in_stock"),
        "category": p.get("category"),
        "description": p.get("description"),
    }


def _pick_view(pick: dict, by_id: dict) -> dict:
    pid = pick.get("product_id") if isinstance(pick, dict) else None
    prod = by_id.get(pid, {})
    return {
        "product_id": pid,
        "quantity": pick.get("quantity") if isinstance(pick, dict) else None,
        "name": prod.get("name"),
        "price_inr": prod.get("price_inr"),
    }


def _display_checks(picks: list, affordable: list[dict], budget_inr: int) -> list[dict]:
    """Human-readable versions of what validate_picks enforces. These are
    for the UI only - validate_picks itself is the authoritative gate."""
    ids = {p["id"] for p in affordable if isinstance(p.get("id"), str)}
    prices = {p["id"]: p["price_inr"] for p in affordable if isinstance(p.get("id"), str)}
    pick_ids = [p.get("product_id") for p in picks if isinstance(p, dict)]

    in_catalog = bool(picks) and all(pid in ids for pid in pick_ids)
    qtys_ok = all(
        isinstance(p.get("quantity"), int)
        and not isinstance(p.get("quantity"), bool)
        and 1 <= p["quantity"] <= MAX_QTY_PER_ITEM
        for p in picks
        if isinstance(p, dict)
    )
    distinct_ok = len(pick_ids) <= MAX_DISTINCT_ITEMS and len(pick_ids) == len(set(pick_ids))
    total = sum(
        prices.get(p["product_id"], 0) * p.get("quantity", 0)
        for p in picks
        if isinstance(p, dict) and p.get("product_id") in prices
    )
    return [
        {"name": f"every pick is a real in-budget catalog item", "ok": in_catalog},
        {"name": f"quantities are 1..{MAX_QTY_PER_ITEM}", "ok": qtys_ok},
        {"name": f"at most {MAX_DISTINCT_ITEMS} distinct items, no duplicates", "ok": distinct_ok},
        {"name": f"cart total (INR {total}) is within budget (INR {budget_inr})", "ok": total <= budget_inr},
    ]


async def run_pipeline(
    goal: str,
    budget_inr: int,
    mercator_endpoint: str,
    llm: LLMSettings,
    *,
    picks_override: list[dict] | None = None,
) -> AsyncIterator[dict]:
    yield {"stage": "start", "goal": goal, "budget_inr": budget_inr}
    _safe_log(
        {"goal": goal, "budget_inr": budget_inr, "mercator_endpoint": mercator_endpoint},
        "goal_received",
    )

    try:
        async with ShopConnection(mercator_endpoint) as session:
            yield {"stage": "connected", "endpoint": mercator_endpoint}

            try:
                catalog = await discover_catalog(session)
            except DiscoverError as exc:
                yield {"stage": "blocked", "at": "discover", "reason": str(exc)}
                return

            affordable = pre_filter(catalog, budget_inr)
            _safe_log(
                {"catalog_size": len(catalog), "affordable_count": len(affordable)},
                "catalog_retrieved",
            )
            yield {
                "stage": "catalog",
                "products": [_product_view(p) for p in catalog],
                "affordable_ids": [p["id"] for p in affordable if isinstance(p.get("id"), str)],
                "affordable_count": len(affordable),
                "total_count": len(catalog),
            }
            if not affordable:
                yield {
                    "stage": "blocked",
                    "at": "filter",
                    "reason": "no in-stock products within budget",
                }
                return

            by_id = {p["id"]: p for p in catalog if isinstance(p.get("id"), str)}

            if picks_override is not None:
                picks = picks_override
                reasoning = "manual override - no LLM call"
                source = "manual-override"
            else:
                yield {"stage": "deciding", "model": llm.model}
                try:
                    decision = await decide(goal, budget_inr, affordable, llm)
                except DecideError as exc:
                    yield {"stage": "blocked", "at": "decide", "reason": f"LLM decision failed: {exc}"}
                    return
                picks = decision.picks
                reasoning = decision.reasoning
                source = "llm"

            _safe_log(
                {"goal": goal, "picks": picks, "reasoning": reasoning, "source": source},
                "llm_decision",
            )
            yield {
                "stage": "decision",
                "source": source,
                "reasoning": reasoning,
                "picks": [_pick_view(pk, by_id) for pk in picks],
            }

            validated = validate_picks(picks, affordable, budget_inr)
            _safe_log(
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
                "validation_result",
            )
            yield {
                "stage": "validation",
                "ok": validated.ok,
                "reason": validated.reason,
                "checks": _display_checks(picks, affordable, budget_inr),
                "total_inr": validated.total_inr,
            }
            if not validated.ok:
                yield {"stage": "blocked", "at": "validate", "reason": validated.reason}
                return

            try:
                pending = await purchase(session, validated)
            except PurchaseError as exc:
                yield {"stage": "blocked", "at": "purchase", "reason": f"purchase failed: {exc}"}
                return

            if isinstance(pending, SettledPurchase):
                # Mercator autopay: settled from a prepaid balance, no link
                # to pay -- money has moved.
                _safe_log(
                    {
                        "total_inr": pending.total_inr,
                        "status": "paid",
                        "settled_via": pending.settled_via,
                    },
                    "checkout_result",
                )
                yield {
                    "stage": "settled",
                    "settled_via": pending.settled_via,
                    "amount": pending.total_inr,
                    "status": "paid",
                }
                return

            _safe_log(
                {
                    "payment_link_id": pending.payment_link_id,
                    "total_inr": pending.total_inr,
                    "status": "pending",
                },
                "checkout_result",
            )
            yield {
                "stage": "pending",
                "payment_link_id": pending.payment_link_id,
                "payment_link_url": pending.payment_link_url,
                "amount": pending.total_inr,
                "expire_hours": pending.expire_hours,
            }
    except Exception as exc:  # noqa: BLE001 - surface, never crash the stream
        current = exc
        while isinstance(current, BaseExceptionGroup) and current.exceptions:
            current = current.exceptions[0]
        yield {
            "stage": "blocked",
            "at": "unexpected",
            "reason": f"{type(current).__name__}: {current}",
        }
    finally:
        yield {"stage": "done"}
