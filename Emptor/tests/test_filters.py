import json
from pathlib import Path

from emptor.filters import pre_filter

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mock_catalog.json"


def _load_catalog():
    return json.loads(FIXTURE_PATH.read_text())


def test_pre_filter_drops_out_of_stock():
    catalog = [
        {"id": "a", "name": "A", "price_inr": 100, "in_stock": True},
        {"id": "b", "name": "B", "price_inr": 100, "in_stock": False},
    ]
    result = pre_filter(catalog, budget_inr=1000)
    assert [p["id"] for p in result] == ["a"]


def test_pre_filter_drops_over_budget():
    catalog = [
        {"id": "a", "name": "A", "price_inr": 100, "in_stock": True},
        {"id": "b", "name": "B", "price_inr": 2000, "in_stock": True},
    ]
    result = pre_filter(catalog, budget_inr=1000)
    assert [p["id"] for p in result] == ["a"]


def test_pre_filter_keeps_item_priced_exactly_at_budget():
    catalog = [{"id": "a", "name": "A", "price_inr": 1000, "in_stock": True}]
    result = pre_filter(catalog, budget_inr=1000)
    assert [p["id"] for p in result] == ["a"]


def test_pre_filter_empty_catalog():
    assert pre_filter([], budget_inr=1000) == []


def test_pre_filter_does_not_mutate_input_catalog():
    catalog = [
        {"id": "a", "name": "A", "price_inr": 100, "in_stock": True},
        {"id": "b", "name": "B", "price_inr": 2000, "in_stock": False},
    ]
    original = [dict(p) for p in catalog]

    pre_filter(catalog, budget_inr=1000)

    assert catalog == original


def test_pre_filter_budget_zero_drops_everything_priced_above_zero():
    catalog = [
        {"id": "a", "name": "A", "price_inr": 0, "in_stock": True},
        {"id": "b", "name": "B", "price_inr": 1, "in_stock": True},
    ]
    result = pre_filter(catalog, budget_inr=0)
    assert [p["id"] for p in result] == ["a"]


def test_pre_filter_against_mock_catalog():
    catalog = _load_catalog()
    result = pre_filter(catalog, budget_inr=1500)
    ids = {p["id"] for p in result}
    assert "sku-004" not in ids  # out of stock
    assert "sku-005" not in ids  # 2200 > 1500 budget
    assert "sku-001" in ids
    assert "sku-002" in ids
