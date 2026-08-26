import json
from pathlib import Path

from emptor.filters import pre_filter
from emptor.validate import MAX_DISTINCT_ITEMS, MAX_QTY_PER_ITEM, validate_picks

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mock_catalog.json"

CATALOG = [
    {"id": "a", "name": "Widget", "price_inr": 100, "in_stock": True},
    {"id": "b", "name": "Gadget", "price_inr": 200, "in_stock": True},
]


def _load_catalog():
    return json.loads(FIXTURE_PATH.read_text())


def test_validate_picks_single_item_within_budget():
    result = validate_picks([{"product_id": "a", "quantity": 1}], CATALOG, budget_inr=1000)
    assert result.ok
    assert result.total_inr == 100
    assert result.items[0].product["id"] == "a"
    assert result.items[0].quantity == 1
    assert result.items[0].line_total_inr == 100


def test_validate_picks_multi_item_within_budget():
    picks = [{"product_id": "a", "quantity": 2}, {"product_id": "b", "quantity": 1}]
    result = validate_picks(picks, CATALOG, budget_inr=1000)
    assert result.ok
    assert result.total_inr == 400
    assert len(result.items) == 2


def test_validate_picks_rejects_unknown_product_id():
    result = validate_picks([{"product_id": "nope", "quantity": 1}], CATALOG, budget_inr=1000)
    assert not result.ok
    assert "nope" in result.reason


def test_validate_picks_rejects_zero_quantity():
    result = validate_picks([{"product_id": "a", "quantity": 0}], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_negative_quantity():
    result = validate_picks([{"product_id": "a", "quantity": -1}], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_non_int_quantity():
    result = validate_picks([{"product_id": "a", "quantity": 1.5}], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_bool_quantity():
    result = validate_picks([{"product_id": "a", "quantity": True}], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_quantity_over_cap():
    result = validate_picks(
        [{"product_id": "a", "quantity": MAX_QTY_PER_ITEM + 1}], CATALOG, budget_inr=100000
    )
    assert not result.ok


def test_validate_picks_rejects_too_many_distinct_items():
    # Create a local catalog with 11 distinct products to actually test the cap
    # (not just the duplicate-id check)
    catalog_with_11 = [
        {"id": f"item-{i}", "name": f"Item {i}", "price_inr": 100, "in_stock": True}
        for i in range(MAX_DISTINCT_ITEMS + 1)
    ]
    picks = [{"product_id": f"item-{i}", "quantity": 1} for i in range(MAX_DISTINCT_ITEMS + 1)]
    result = validate_picks(picks, catalog_with_11, budget_inr=100000)
    assert not result.ok


def test_validate_picks_rejects_duplicate_product_id():
    picks = [{"product_id": "a", "quantity": 1}, {"product_id": "a", "quantity": 1}]
    result = validate_picks(picks, CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_accepts_total_exactly_equal_to_budget():
    picks = [{"product_id": "a", "quantity": 1}, {"product_id": "b", "quantity": 1}]
    result = validate_picks(picks, CATALOG, budget_inr=300)
    assert result.ok
    assert result.total_inr == 300


def test_validate_picks_accepts_max_distinct_items_at_max_qty():
    catalog = [
        {"id": f"item-{i}", "name": f"Item {i}", "price_inr": 10, "in_stock": True}
        for i in range(MAX_DISTINCT_ITEMS)
    ]
    picks = [
        {"product_id": f"item-{i}", "quantity": MAX_QTY_PER_ITEM} for i in range(MAX_DISTINCT_ITEMS)
    ]
    result = validate_picks(picks, catalog, budget_inr=100000)
    assert result.ok
    assert len(result.items) == MAX_DISTINCT_ITEMS
    assert result.total_inr == MAX_DISTINCT_ITEMS * MAX_QTY_PER_ITEM * 10


def test_validate_picks_rejects_string_quantity_no_coercion():
    result = validate_picks([{"product_id": "a", "quantity": "2"}], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_pick_not_present_in_catalog_it_was_given():
    # validate_picks's contract is: validate against the catalog list it was
    # handed, not some separate "original" snapshot elsewhere. A product that
    # exists in a *different* catalog list is still rejected here, because it
    # isn't in the list actually passed in.
    other_catalog = [{"id": "c", "name": "Not passed in", "price_inr": 50, "in_stock": True}]
    assert other_catalog[0]["id"] not in {p["id"] for p in CATALOG}

    result = validate_picks([{"product_id": "c", "quantity": 1}], CATALOG, budget_inr=1000)

    assert not result.ok
    assert "c" in result.reason


def test_validate_picks_rejects_over_budget_total():
    picks = [{"product_id": "a", "quantity": 1}, {"product_id": "b", "quantity": 1}]
    result = validate_picks(picks, CATALOG, budget_inr=250)
    assert not result.ok
    assert result.total_inr == 300


def test_validate_picks_rejects_empty_list():
    result = validate_picks([], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_non_dict_pick():
    result = validate_picks(["a"], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_pick_missing_product_id():
    result = validate_picks([{"quantity": 1}], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_pick_missing_quantity():
    result = validate_picks([{"product_id": "a"}], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_rejects_non_string_product_id():
    result = validate_picks([{"product_id": ["a"], "quantity": 1}], CATALOG, budget_inr=1000)
    assert not result.ok


def test_validate_picks_multi_item_using_mock_catalog():
    catalog = _load_catalog()
    affordable = pre_filter(catalog, budget_inr=1500)
    picks = [{"product_id": "sku-001", "quantity": 2}, {"product_id": "sku-002", "quantity": 3}]
    result = validate_picks(picks, affordable, budget_inr=1500)
    assert result.ok
    assert result.total_inr == 120 * 2 + 80 * 3
