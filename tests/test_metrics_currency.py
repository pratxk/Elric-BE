"""Metrics/aging aggregates never sum across currencies (no FX in the
demo): a single-currency set reports its own currency; a mixed set
reports the INR subset only, labelled INR."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import make_client

client = TestClient(app)


@pytest.fixture(autouse=True)
def _authed_client():
    global client
    client = make_client()
    yield


def _seed(rows):
    from app.infra.store import store

    today = date.today()
    uid = client.user_id
    for i, r in enumerate(rows):
        store.create_invoice(
            {
                "id": f"inv-cur-{i}",
                "user_id": uid,
                "vendor_id": "vendor-a",
                "vendor_name": "Vendor A",
                "invoice_number": f"A-{i}",
                "invoice_date": str(today),
                "due_date": str(r["due"]),
                "status": r["status"],
                "total": str(r["total"]),
                "currency": r.get("currency", "INR"),
            }
        )


def test_metrics_single_usd_exception_reports_usd():
    today = date.today()
    _seed(
        [
            {"due": today - timedelta(days=10), "status": "flagged",
             "total": "372.49", "currency": "$"},
        ]
    )
    body = client.get("/api/metrics").json()
    assert body["money_in_exceptions_inr"] == "372.49"
    assert body["money_in_exceptions_currency"] == "USD"
    assert body["overdue_total_currency"] == "USD"
    assert body["overdue_total_inr"] == "372.49"


def test_metrics_mixed_currencies_reports_inr_subset_only():
    today = date.today()
    _seed(
        [
            {"due": today - timedelta(days=10), "status": "flagged",
             "total": "372.49", "currency": "$"},
            {"due": today - timedelta(days=5), "status": "flagged",
             "total": "1000", "currency": "INR"},
        ]
    )
    body = client.get("/api/metrics").json()
    assert body["money_in_exceptions_currency"] == "INR"
    assert body["money_in_exceptions_inr"] == "1000.00"
    assert body["overdue_total_currency"] == "INR"
    assert body["overdue_total_inr"] == "1000.00"


def test_metrics_due_soon_currency_follows_its_invoices():
    today = date.today()
    _seed(
        [
            {"due": today + timedelta(days=3), "status": "matched",
             "total": "250", "currency": "£"},
        ]
    )
    body = client.get("/api/metrics").json()
    assert body["due_soon_total_currency"] == "GBP"
    assert body["due_soon_total_inr"] == "250.00"


def test_aging_endpoint_reports_currency():
    today = date.today()
    _seed(
        [
            {"due": today - timedelta(days=10), "status": "flagged",
             "total": "372.49", "currency": "$"},
        ]
    )
    body = client.get("/api/aging").json()
    assert body["overdue_total_currency"] == "USD"
    assert body["overdue_total_inr"] == "372.49"
