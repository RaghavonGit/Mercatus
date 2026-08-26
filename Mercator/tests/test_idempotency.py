from unittest.mock import Mock

from mercator import reasons
from mercator.guardrails import check_idempotency_key_present
from mercator.idempotency import IdempotencyStore, run_with_idempotency


def test_store_has_false_for_unknown_key():
    store = IdempotencyStore()
    assert store.has("missing-key") is False


def test_store_set_then_get_returns_stored_result():
    store = IdempotencyStore()
    store.set("key-1", {"ok": True, "order_id": "order_1"})
    assert store.has("key-1") is True
    assert store.get("key-1") == {"ok": True, "order_id": "order_1"}


def test_store_get_returns_copy_not_same_object():
    store = IdempotencyStore()
    store.set("key-1", {"ok": True, "order_id": "order_1"})
    fetched = store.get("key-1")
    fetched["order_id"] = "tampered"
    assert store.get("key-1") == {"ok": True, "order_id": "order_1"}


def test_store_get_unknown_key_returns_none():
    store = IdempotencyStore()
    assert store.get("missing-key") is None


def test_missing_idempotency_key_rejected():
    store = IdempotencyStore()
    operation = Mock(return_value={"ok": True, "order_id": "order_1"})
    result = run_with_idempotency(store, None, operation)
    assert result == {"ok": False, "reason": reasons.MISSING_IDEMPOTENCY_KEY}
    operation.assert_not_called()


def test_non_str_idempotency_key_rejected():
    store = IdempotencyStore()
    operation = Mock(return_value={"ok": True})
    result = run_with_idempotency(store, 123, operation)
    assert result["reason"] == reasons.MISSING_IDEMPOTENCY_KEY
    operation.assert_not_called()


def test_whitespace_only_idempotency_key_rejected():
    store = IdempotencyStore()
    operation = Mock(return_value={"ok": True})
    result = run_with_idempotency(store, "   ", operation)
    assert result["reason"] == reasons.MISSING_IDEMPOTENCY_KEY
    operation.assert_not_called()


def test_missing_key_reason_matches_guardrails_constant():
    store = IdempotencyStore()
    idempotency_result = run_with_idempotency(store, None, Mock())
    guardrail_result = check_idempotency_key_present(None)
    assert idempotency_result["reason"] == guardrail_result.reason


def test_new_key_runs_operation_and_stores_result():
    store = IdempotencyStore()
    operation = Mock(return_value={"ok": True, "order_id": "order_1"})
    result = run_with_idempotency(store, "key-1", operation)
    assert result == {"ok": True, "order_id": "order_1"}
    assert operation.call_count == 1
    assert store.get("key-1") == {"ok": True, "order_id": "order_1"}


def test_repeated_key_returns_stored_result_operation_not_called_again():
    store = IdempotencyStore()
    operation = Mock(return_value={"ok": True, "order_id": "order_1"})
    first = run_with_idempotency(store, "key-1", operation)
    second = run_with_idempotency(store, "key-1", operation)
    assert first == second
    assert operation.call_count == 1


def test_two_different_keys_same_cart_two_distinct_attempts():
    store = IdempotencyStore()
    operation = Mock(
        side_effect=[
            {"ok": True, "order_id": "order_1"},
            {"ok": True, "order_id": "order_2"},
        ]
    )
    first = run_with_idempotency(store, "key-1", operation)
    second = run_with_idempotency(store, "key-2", operation)
    assert operation.call_count == 2
    assert first["order_id"] == "order_1"
    assert second["order_id"] == "order_2"


def test_rejection_result_also_stored_and_replayed():
    store = IdempotencyStore()
    operation = Mock(return_value={"ok": False, "reason": reasons.SPEND_CAP_EXCEEDED})
    first = run_with_idempotency(store, "key-1", operation)
    second = run_with_idempotency(store, "key-1", operation)
    assert first == second == {"ok": False, "reason": reasons.SPEND_CAP_EXCEEDED}
    assert operation.call_count == 1


def test_oversized_idempotency_key_rejected():
    from mercator.limits import MAX_ID_LENGTH

    store = IdempotencyStore()
    operation = Mock(return_value={"ok": True})
    result = run_with_idempotency(store, "a" * (MAX_ID_LENGTH + 1), operation)
    assert result["reason"] == reasons.MISSING_IDEMPOTENCY_KEY
    operation.assert_not_called()


def test_oversized_key_reason_matches_guardrails_constant():
    from mercator.limits import MAX_ID_LENGTH

    oversized = "a" * (MAX_ID_LENGTH + 1)
    idempotency_result = run_with_idempotency(IdempotencyStore(), oversized, Mock())
    guardrail_result = check_idempotency_key_present(oversized)
    assert idempotency_result["reason"] == guardrail_result.reason


def test_store_full_rejects_new_key_without_running_operation():
    store = IdempotencyStore(max_entries=1)
    store.set("existing-key", {"ok": True})
    operation = Mock(return_value={"ok": True, "order_id": "order_2"})
    result = run_with_idempotency(store, "brand-new-key", operation)
    assert result == {"ok": False, "reason": reasons.INVALID_INPUT, "detail": "Too many pending idempotency keys"}
    operation.assert_not_called()


def test_store_full_still_replays_an_existing_key():
    store = IdempotencyStore(max_entries=1)
    operation = Mock(return_value={"ok": True, "order_id": "order_1"})
    first = run_with_idempotency(store, "existing-key", operation)
    second = run_with_idempotency(store, "existing-key", operation)
    assert first == second == {"ok": True, "order_id": "order_1"}
    assert operation.call_count == 1
