"""API tests — health, invoice CRUD, exceptions queue, metrics, eval run."""

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_invoices_empty_or_list():
    resp = client.get("/api/invoices")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_upload_and_get_invoice():
    files = {"file": ("inv.pdf", io.BytesIO(b"%PDF-fake-content-1"), "application/pdf")}
    resp = client.post("/api/invoices/upload", files=files)
    assert resp.status_code == 202, resp.text
    invoice_id = resp.json()["id"]
    assert resp.json()["status"] == "received"

    got = client.get(f"/api/invoices/{invoice_id}")
    assert got.status_code == 200
    assert got.json()["id"] == invoice_id


def test_upload_parks_invoice_on_hold_without_extraction_key():
    # No GEMINI_API_KEY: extraction fails, the graph routes straight to
    # audit and the invoice parks on_hold — never faked, never matched
    # on unextracted data.
    files = {"file": ("inv.pdf", io.BytesIO(b"%PDF-parked-flagged"), "application/pdf")}
    resp = client.post("/api/invoices/upload", files=files)
    assert resp.status_code == 202
    got = client.get(f"/api/invoices/{resp.json()['id']}")
    assert got.json()["status"] == "on_hold"


def test_upload_duplicate_hash_rejected():
    files = {"file": ("inv.pdf", io.BytesIO(b"%PDF-dupe-content"), "application/pdf")}
    first = client.post("/api/invoices/upload", files=files)
    assert first.status_code == 202
    second = client.post("/api/invoices/upload", files=files)
    assert second.status_code == 409


def test_upload_rejects_unsupported_type():
    files = {"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")}
    resp = client.post("/api/invoices/upload", files=files)
    assert resp.status_code == 415


def test_get_missing_invoice_404():
    assert client.get("/api/invoices/does-not-exist").status_code == 404


def test_query_vendor_is_draft_only():
    files = {"file": ("inv.pdf", io.BytesIO(b"%PDF-query-draft"), "application/pdf")}
    invoice_id = client.post("/api/invoices/upload", files=files).json()["id"]
    resp = client.post(f"/api/invoices/{invoice_id}/query-vendor")
    assert resp.status_code == 200
    assert resp.json()["sent"] is False
    assert "Query on invoice" in resp.json()["draft"]


def test_eval_run_reports_zero_false_approves():
    resp = client.post("/api/eval/run", params={"n_per_scenario": 1})
    assert resp.status_code == 200
    metrics = resp.json()["metrics"]
    assert metrics["n_cases"] == 7
    assert metrics["false_approves"] == 0
    assert metrics["false_approve_rate"] == 0.0
    assert metrics["match_rate"] > 0


def test_metrics_endpoint():
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_invoices" in body
    assert "by_status" in body
    assert "money_in_exceptions_inr" in body


def test_exception_queue_endpoint():
    resp = client.get("/api/queue/exceptions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
