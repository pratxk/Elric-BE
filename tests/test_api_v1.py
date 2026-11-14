"""API v1 end-to-end tests — mocked extraction, real HTTP + graph + store.

Flow: seed a PO via the API → upload an invoice (fake Gemini extraction)
→ variance case pauses flagged → approve resumes the graph → bookkeeper
posts balanced ledger entries + schedules payment. Also covers aging,
forecast, ledger/payments endpoints and the graph-backed eval run.
"""

import io

from fastapi.testclient import TestClient

from app.agents import extractor as extractor_mod
from app.eval.synthetic import generate_corpus
from app.main import app
from app.services.extraction_service import ExtractionService
from tests.test_extraction import FakeClient, _perfect_payload

client = TestClient(app)


def _case(scenario):
    return next(
        c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == scenario
    )


def _payload_for(case):
    payload = _perfect_payload()
    inv = case["invoice"]
    payload["invoice_number"] = inv.invoice_number
    payload["invoice_date"] = str(inv.invoice_date)
    payload["due_date"] = str(inv.due_date)
    payload["lines"] = [
        {
            "description": ln.description,
            "hsn_code": ln.hsn_code,
            "qty": float(ln.qty),
            "unit_price": float(ln.unit_price),
            "tax_rate_pct": float(ln.tax_rate_pct),
        }
        for ln in inv.lines
    ]
    payload["subtotal"] = float(inv.subtotal)
    payload["tax_total"] = float(inv.tax_total)
    payload["total"] = float(inv.total)
    payload["cgst"] = float(inv.cgst)
    payload["sgst"] = float(inv.sgst)
    payload["igst"] = float(inv.igst)
    return payload


def _seed_po(case):
    po = case["po"]
    resp = client.post(
        "/api/purchase-orders",
        json={
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "vendor_name": "Acme Corporation Pvt Ltd",
            "po_date": str(po.po_date),
            "total": str(po.total),
            "lines": [
                {
                    "description": ln.description,
                    "hsn_code": ln.hsn_code,
                    "qty": str(ln.qty),
                    "unit_price": str(ln.unit_price),
                    "tax_rate_pct": str(ln.tax_rate_pct),
                }
                for ln in po.lines
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _upload_with_fake(case, payload):
    import uuid as _uuid

    old = extractor_mod._service
    extractor_mod._service = ExtractionService(client=FakeClient([payload]))
    try:
        files = {
            "file": (
                "inv.pdf",
                io.BytesIO(b"%PDF-e2e-" + _uuid.uuid4().hex.encode()),
                "application/pdf",
            )
        }
        resp = client.post("/api/invoices/upload", files=files)
        assert resp.status_code == 202, resp.text
        return resp.json()["id"]
    finally:
        extractor_mod._service = old


def _posted_invoice_id():
    """Self-contained setup: seed a PO and upload a perfect invoice that
    auto-posts. Returns the invoice id."""
    case = _case("perfect")
    _seed_po(case)
    invoice_id = _upload_with_fake(case, _payload_for(case))
    assert client.get(f"/api/invoices/{invoice_id}").json()["status"] == "posted"
    return invoice_id


def test_e2e_variance_upload_approve_books_ledger():
    case = _case("price_variance")
    _seed_po(case)
    invoice_id = _upload_with_fake(case, _payload_for(case))

    got = client.get(f"/api/invoices/{invoice_id}").json()
    assert got["status"] == "flagged"
    assert "PRICE_VARIANCE" in [e["code"] for e in got["exceptions"]]
    # Root cause landed on the exception record.
    assert got["exceptions"][0].get("root_cause")

    # Exception queue shows it; trace streams the live node events.
    queue = client.get("/api/queue/exceptions").json()
    assert invoice_id in [r["id"] for r in queue]
    trace = client.get(f"/api/invoices/{invoice_id}/trace")
    assert trace.status_code == 200
    assert "node:match" in trace.text
    assert "node:exception" in trace.text

    # Vendor query is a draft only.
    draft = client.post(f"/api/invoices/{invoice_id}/query-vendor").json()
    assert draft["sent"] is False
    assert "PRICE_VARIANCE" in draft["draft"]

    # Human approves → the paused graph resumes → bookkeeper posts.
    approved = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"approver": "ap_manager", "reason": "vendor confirmed new price"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "posted"

    ledger = client.get(f"/api/invoices/{invoice_id}/ledger").json()
    assert len(ledger) == 2
    debits = sum(float(e["debit"]) for e in ledger)
    credits = sum(float(e["credit"]) for e in ledger)
    assert debits == credits == float(case["invoice"].total)
    payments = client.get(f"/api/invoices/{invoice_id}/payments").json()
    assert len(payments) == 1
    assert payments[0]["status"] == "scheduled"

    # Second approve is a conflict (already posted), not a double-post.
    assert client.post(
        f"/api/invoices/{invoice_id}/approve", json={}
    ).status_code == 409
    assert len(client.get(f"/api/invoices/{invoice_id}/ledger").json()) == 2


def test_e2e_perfect_upload_auto_posts():
    case = _case("perfect")
    _seed_po(case)
    invoice_id = _upload_with_fake(case, _payload_for(case))
    got = client.get(f"/api/invoices/{invoice_id}").json()
    assert got["status"] == "posted"
    assert got["match"]["outcome"] == "clean"
    assert len(client.get(f"/api/invoices/{invoice_id}/ledger").json()) == 2


def test_e2e_reject_ends_rejected_without_ledger():
    case = _case("price_variance")
    _seed_po(case)
    invoice_id = _upload_with_fake(case, _payload_for(case))
    rejected = client.post(
        f"/api/invoices/{invoice_id}/reject",
        json={"approver": "ap_manager", "reason": "price not agreed"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert client.get(f"/api/invoices/{invoice_id}/ledger").json() == []


def test_aging_and_forecast_endpoints():
    posted_id = _posted_invoice_id()
    resp = client.get("/api/aging")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["buckets"]) == {
        "current", "overdue_1_30", "overdue_31_60", "overdue_61_90", "overdue_90_plus",
    }
    assert "overdue_total_inr" in body

    resp = client.get("/api/forecast", params={"weeks": 4, "weekly_budget_inr": "1"})
    assert resp.status_code == 200
    assert len(resp.json()["weekly_outflows"]) == 4
    assert "over_budget_weeks" in resp.json()

    # Discount endpoint: real NPV math through the API.
    rec = client.post(
        "/api/forecast/discount",
        json={"invoice_id": posted_id, "terms": "2/10 Net 30"},
    )
    assert rec.status_code == 200
    assert rec.json()["recommendation"] in ("pay_early", "pay_on_due_date")
    bad = client.post(
        "/api/forecast/discount",
        json={"invoice_id": posted_id, "terms": "Net 30"},
    )
    assert bad.status_code == 422


def test_eval_run_includes_graph_eval():
    resp = client.post("/api/eval/run", params={"n_per_scenario": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["metrics"]["n_cases"] == 7
    assert body["metrics"]["false_approves"] == 0
    graph_eval = body["graph_eval"]
    assert graph_eval is not None
    assert graph_eval["n_cases"] == 7
    assert graph_eval["false_approves"] == 0
    assert graph_eval["posted"] >= 1  # the perfect case posts end-to-end
    assert graph_eval["flagged"] >= 1  # the mismatch cases pause for humans


def test_metrics_reports_real_numbers():
    _posted_invoice_id()
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_invoices"] >= 1
    assert body["match_rate"] > 0
    assert body["auto_approved"] >= 1
    assert "median_cycle_time_seconds" in body
    assert "overdue_count" in body
