"""The `uv run demo --verify` checklist must stay green -- it is the
headless end-to-end proof of the autopay path (config invariants, an
autopay settle with zero gateway calls, the ledger's human_approval field,
the payment-link fallback, idempotent replay, concurrent no-overdraw, chain
integrity). No servers, no LLM, no Razorpay."""

import forum.demo as demo


def test_verify_all_checks_pass(capsys):
    code = demo._verify()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "FAIL" not in out
    # a few of the properties are named explicitly so a regression is legible
    assert "settles via autopay" in out
    assert "zero payment-gateway calls" in out
    assert "human_approval: false" in out
    assert "falls back to a payment link" in out
    assert "balance never negative" in out
    assert "hash chain is intact" in out
