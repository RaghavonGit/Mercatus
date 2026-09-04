from mercator.guardrails import GuardrailResult, run_all_guardrails
from mercator.ledger import Ledger


def test_log_writes_one_entry(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log("custom_event", {"foo": "bar"})
    assert entry.event_type == "custom_event"
    assert entry.actor == "mercator"
    assert entry.data == {"foo": "bar"}
    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 1


def test_log_appends_multiple_entries(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry_a = ledger.log("event_a", {})
    entry_b = ledger.log("event_b", {})
    assert [entry_a.event_type, entry_b.event_type] == ["event_a", "event_b"]
    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 2


def test_ledger_creates_file_if_not_exists(tmp_path):
    path = tmp_path / "nested" / "ledger.db"
    path.parent.mkdir()
    ledger = Ledger(path)
    ledger.log("event_a", {})
    assert path.exists()


def test_log_guardrail_check_passed(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log_guardrail_check("check_spend_cap", GuardrailResult(passed=True))
    assert entry.event_type == "guardrail_check"
    assert entry.data["check"] == "check_spend_cap"
    assert entry.data["passed"] is True
    assert entry.data["reason"] is None


def test_log_guardrail_check_failed_includes_reason_and_detail(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log_guardrail_check(
        "check_spend_cap",
        GuardrailResult(passed=False, reason="SPEND_CAP_EXCEEDED", detail="Cart total 2000 exceeds cap 1500"),
    )
    assert entry.data["passed"] is False
    assert entry.data["reason"] == "SPEND_CAP_EXCEEDED"
    assert entry.data["detail"] == "Cart total 2000 exceeds cap 1500"


def test_log_checkout_result_success(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log_checkout_result(
        "cart_1", "idem-key-1", {"ok": True, "order_id": "order_1", "amount": 450, "status": "created"}
    )
    assert entry.event_type == "checkout_result"
    assert entry.data["cart_id"] == "cart_1"
    assert entry.data["idempotency_key"] == "idem-key-1"
    assert entry.data["ok"] is True


def test_log_checkout_result_rejection(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log_checkout_result("cart_1", "idem-key-1", {"ok": False, "reason": "SPEND_CAP_EXCEEDED"})
    assert entry.data["ok"] is False
    assert entry.data["reason"] == "SPEND_CAP_EXCEEDED"


def test_log_checkout_result_persists_status_link_and_amount(tmp_path):
    # Was live-mode blocker (a): these fields were dropped, so paid vs
    # cancelled vs expired vs failed were indistinguishable in the ledger.
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log_checkout_result(
        "cart_1",
        "idem-key-1",
        {
            "ok": True,
            "payment_link_id": "plink_9",
            "amount": 450,
            "status": "paid",
        },
    )
    assert entry.data["status"] == "paid"
    assert entry.data["payment_link_id"] == "plink_9"
    assert entry.data["amount"] == 450


def test_log_checkout_result_persists_detail_on_failure(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log_checkout_result(
        "cart_1", "idem-key-1", {"ok": False, "reason": "PAYMENT_FAILED", "detail": "create raised Timeout"}
    )
    assert entry.data["detail"] == "create raised Timeout"


def test_log_autopay_result_settled_marks_no_human_approval(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log_autopay_result(
        "cart_1",
        "idem-key-1",
        outcome="autopay_settled",
        amount_inr=300,
        autopay_threshold_inr=800,
        autopay_allowed_categories=["books"],
        balance_before_inr=5000,
        balance_after_inr=4700,
    )
    assert entry.event_type == "autopay_result"
    assert entry.data["cart_id"] == "cart_1"
    assert entry.data["idempotency_key"] == "idem-key-1"
    assert entry.data["outcome"] == "autopay_settled"
    assert entry.data["human_approval"] is False
    assert entry.data["replay"] is False
    assert entry.data["amount_inr"] == 300
    assert entry.data["balance_before_inr"] == 5000
    assert entry.data["balance_after_inr"] == 4700
    assert entry.data["autopay_threshold_inr"] == 800
    assert entry.data["autopay_allowed_categories"] == ["books"]
    assert entry.data["fallback_cause"] is None
    assert ledger.verify_chain().is_valid is True


def test_log_autopay_result_fallback_records_cause(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    entry = ledger.log_autopay_result(
        "cart_1",
        "idem-key-1",
        outcome="fell_back_to_manual",
        amount_inr=1200,
        autopay_threshold_inr=800,
        autopay_allowed_categories=["books"],
        fallback_cause="AUTOPAY_OVER_THRESHOLD",
    )
    assert entry.data["outcome"] == "fell_back_to_manual"
    assert entry.data["human_approval"] is False
    assert entry.data["fallback_cause"] == "AUTOPAY_OVER_THRESHOLD"
    assert entry.data["balance_before_inr"] is None


def test_ledger_log_fn_records_all_six_guardrail_checks(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    cart = {"cart_id": "cart_1", "items": []}
    config = {"spend_cap_inr": 1500, "allowed_categories": ["books"]}
    recorded = []
    run_all_guardrails(
        cart,
        "idem-key-1",
        config,
        log_fn=lambda name, result: recorded.append(ledger.log_guardrail_check(name, result)),
    )
    assert len(recorded) == 6
    assert all(entry.event_type == "guardrail_check" for entry in recorded)
    result = ledger.verify_chain()
    assert result.is_valid is True
    assert result.entries_checked == 6
