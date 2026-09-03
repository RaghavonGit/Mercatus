"""API-surface smoke tests via FastAPI's TestClient. Razorpay and the shop
are stubbed."""

import dataclasses

import forum.app as app_module
from fastapi.testclient import TestClient

client = TestClient(app_module.app)


def _with_config(monkeypatch, **overrides):
    monkeypatch.setattr(app_module, "CONFIG", dataclasses.replace(app_module.CONFIG, **overrides))


def test_index_serves_the_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "Mercatus" in r.text


def test_payment_endpoint_passes_through_fetch_payment_link(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "fetch_payment_link",
        lambda cli, link_id: {"payment_link_id": link_id, "status": "paid", "amount": 450, "amount_paid": 450},
    )
    r = client.get("/api/payment/plink_abc")
    assert r.status_code == 200
    assert r.json() == {
        "payment_link_id": "plink_abc",
        "status": "paid",
        "amount": 450,
        "amount_paid": 450,
    }


def test_payment_endpoint_502s_on_razorpay_error(monkeypatch):
    def boom(cli, link_id):
        raise RuntimeError("network")

    monkeypatch.setattr(app_module, "fetch_payment_link", boom)
    r = client.get("/api/payment/x")
    assert r.status_code == 502


def test_spend_endpoint_without_a_db_returns_zero(monkeypatch, tmp_path):
    _with_config(monkeypatch, mercator_spend_db=tmp_path / "nope.db", mercator_dir=tmp_path)
    r = client.get("/api/spend")
    assert r.status_code == 200
    assert r.json()["paid_inr_24h"] == 0


def test_ledger_endpoint_shape(monkeypatch, tmp_path):
    _with_config(monkeypatch, mercator_ledger_db=tmp_path / "m.db", emptor_ledger_db=tmp_path / "e.db")
    r = client.get("/api/ledger")
    body = r.json()
    assert r.status_code == 200
    assert body["events"] == []
    assert set(body["chains"]) == {"mercator", "emptor"}
