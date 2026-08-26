"""Hashing-core tests: determinism, sensitivity, None/missing handling.

Covers CLAUDE.md section 11 checklist items:
- Same entry data -> same hash, every time (determinism)
- Changing any single field (even whitespace in `data`) -> different hash
"""

import json
from pathlib import Path

import pytest

from fides.entry import canonical_data_json, compute_hash

FIXTURES_DIR = Path(__file__).parent / "fixtures"

BASE_ARGS = dict(
    seq=1,
    timestamp="2026-08-25T10:00:00+00:00",
    actor="emptor",
    event_type="goal_received",
    prev_hash=None,
)


def _hash(data: dict, **overrides) -> str:
    args = {**BASE_ARGS, **overrides}
    data_json = canonical_data_json(data)
    return compute_hash(
        args["seq"],
        args["timestamp"],
        args["actor"],
        args["event_type"],
        data_json,
        args["prev_hash"],
    )


def test_determinism_same_input_same_hash():
    data = {"goal": "birthday gift", "budget_inr": 1500}
    h1 = _hash(data)
    h2 = _hash(data)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hexdigest length


def test_determinism_across_many_calls():
    data = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    hashes = {_hash(data) for _ in range(20)}
    assert len(hashes) == 1


@pytest.mark.parametrize(
    "field,override",
    [
        ("seq", {"seq": 2}),
        ("timestamp", {"timestamp": "2026-08-25T10:00:01+00:00"}),
        ("actor", {"actor": "mercator"}),
        ("event_type", {"event_type": "catalog_retrieved"}),
        ("prev_hash", {"prev_hash": "deadbeef"}),
    ],
)
def test_sensitivity_top_level_field_change(field, override):
    data = {"goal": "birthday gift", "budget_inr": 1500}
    baseline = _hash(data)
    changed = _hash(data, **override)
    assert baseline != changed, f"changing {field} did not change the hash"


def test_sensitivity_data_value_change():
    baseline = _hash({"goal": "birthday gift", "budget_inr": 1500})
    changed = _hash({"goal": "birthday gift", "budget_inr": 1501})
    assert baseline != changed


def test_sensitivity_whitespace_inside_string_value():
    baseline = _hash({"note": "hello world"})
    changed = _hash({"note": "hello  world"})  # two spaces
    assert baseline != changed


def test_sensitivity_trailing_whitespace_in_string_value():
    baseline = _hash({"note": "hello"})
    changed = _hash({"note": "hello "})
    assert baseline != changed


def test_key_order_in_caller_dict_does_not_affect_hash():
    h1 = _hash({"a": 1, "b": 2, "c": 3})
    h2 = _hash({"c": 3, "a": 1, "b": 2})
    assert h1 == h2, "sort_keys=True should make caller key order irrelevant"


def test_none_value_differs_from_missing_key():
    with_none = canonical_data_json({"x": None})
    without_key = canonical_data_json({})
    assert with_none != without_key
    assert with_none == '{"x":null}'
    assert without_key == "{}"

    h_with_none = _hash({"x": None})
    h_without_key = _hash({})
    assert h_with_none != h_without_key


def test_none_vs_missing_nested_field():
    h1 = _hash({"a": 1, "b": None})
    h2 = _hash({"a": 1})
    assert h1 != h2


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_rejected(bad_value):
    with pytest.raises(ValueError):
        canonical_data_json({"x": bad_value})


def test_non_serializable_value_rejected():
    class NotSerializable:
        pass

    with pytest.raises(TypeError):
        canonical_data_json({"x": NotSerializable()})


def test_canonical_data_json_is_compact_and_sorted():
    result = canonical_data_json({"b": 2, "a": 1})
    assert result == '{"a":1,"b":2}'
    assert " " not in result


def test_first_entry_prev_hash_none_is_valid_input():
    # Should not raise -- None is the documented value for the first entry.
    h = _hash({"goal": "gift"}, prev_hash=None)
    assert isinstance(h, str) and len(h) == 64


def test_frozen_fixture_hashes_match_recomputation():
    """The one test that would actually catch a future accidental change to
    the canonical serialization format. `entry_hash` values in
    sample_entries.json were computed once by this exact code and then
    hardcoded there -- this test recomputes them independently and compares
    against those frozen, hardcoded literals (not against a value the test
    itself just computed) to prove the format hasn't silently drifted.
    """
    entries = json.loads((FIXTURES_DIR / "sample_entries.json").read_text())
    for entry in entries:
        data_json = canonical_data_json(entry["data"])
        recomputed = compute_hash(
            entry["seq"],
            entry["timestamp"],
            entry["actor"],
            entry["event_type"],
            data_json,
            entry["prev_hash"],
        )
        assert recomputed == entry["entry_hash"], (
            f"seq={entry['seq']}: recomputed hash does not match the frozen "
            "fixture value -- the canonical serialization format has "
            "changed since the fixture was frozen"
        )


def test_frozen_fixture_chain_is_linked_correctly():
    entries = json.loads((FIXTURES_DIR / "sample_entries.json").read_text())
    assert entries[0]["prev_hash"] is None
    for prev_entry, entry in zip(entries, entries[1:]):
        assert entry["prev_hash"] == prev_entry["entry_hash"]
