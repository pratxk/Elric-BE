"""Fix-round tests (v1.1): extraction failure surfacing, retry endpoint,
vendor-registry list. Mocked extraction only — zero real Gemini calls.

- A 503 overload must surface on the invoice as extraction_error /
  extraction_error_kind="overloaded" (never as "no key").
- A missing key must surface as kind="no_key".
- POST /api/invoices/{id}/retry-extraction re-runs the pipeline for an
  on_hold invoice (202), clears the stale error, and refuses when the
  document bytes are gone (422) or the status is not retryable (409).
- GET /api/vendors/registry lists the user's registered vendors
  (per-user), including vendors with no invoices yet.
"""

import io
import uuid

import pytest
from fastapi.testclient import TestClient

from app.agents import extractor as extractor_mod
from app.eval.synthetic import generate_corpus
from app.infra import docstore
from app.infra.gemini_client import GeminiTransientError
from app.main import app
from app.services.extraction_service import ExtractionService
from tests.helpers import make_client
from tests.test_api_v1 import _payload_for
from tests.test_extraction import FakeClient

client = TestClient(app)


def _seed_po(case):
    """Seed the case's PO via THIS module's authed client (importing
    test_api_v1._seed_po would post as that module's client/user)."""
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


@pytest.fixture(autouse=True)
def _authed_client():
    global client
    client = make_client()
    yield


class FailingClient:
    """Fake Gemini client that always fails with the given exception."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    @property
    def configured(self):
        return True

    def generate_structured(self, prompt, schema, **kwargs):
        self.calls += 1
        raise self._exc


def _upload(files_name=b"%PDF-retry-case"):
    files = {
        "file": (
            "inv.pdf",
            io.BytesIO(files_name + uuid.uuid4().hex.encode()),
            "application/pdf",
        )
    }
    resp = client.post("/api/invoices/upload", files=files)
    assert resp.status_code == 202, resp.text
    return resp.json()["id"]


def test_upload_503_surfaces_overloaded_not_no_key():
    """The verifier's case: gemini-3.8-flash 503s. The invoice must park
    on_hold carrying the TRUE reason — kind 'overloaded', message about
    overload — and the UI must never see a 'no key' claim for it."""
    old = extractor_mod._service
    extractor_mod._service = ExtractionService(
        client=FailingClient(
            GeminiTransientError(
                503, "Gemini overloaded (HTTP 503) — the model is temporarily unavailable; try again in a few minutes"
            )
        )
    )
    try:
        invoice_id = _upload()
    finally:
        extractor_mod._service = old
    got = client.get(f"/api/invoices/{invoice_id}").json()
    assert got["status"] == "on_hold"
    assert got["extraction_error_kind"] == "overloaded"
    assert "overloaded" in got["extraction_error"].lower()
    assert "no key" not in got["extraction_error"].lower()


def test_upload_without_key_surfaces_no_key_kind():
    """No GEMINI_API_KEY (conftest unsets both keys): the parked invoice
    honestly reports kind 'no_key' — the one case where the UI may say
    the key is missing."""
    invoice_id = _upload()
    got = client.get(f"/api/invoices/{invoice_id}").json()
    assert got["status"] == "on_hold"
    assert got["extraction_error_kind"] == "no_key"
    assert "no gemini api key" in got["extraction_error"].lower()


def test_retry_extraction_recovers_after_503():
    """Seed a PO, upload with a 503-failing client (parks on_hold with
    the overload error), then retry with a working fake: the invoice
    extracts, matches clean, auto-approves and posts — and the stale
    overload error is gone."""
    case = next(c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == "perfect")
    _seed_po(case)
    old = extractor_mod._service
    extractor_mod._service = ExtractionService(
        client=FailingClient(GeminiTransientError(503, "Gemini overloaded (HTTP 503)"))
    )
    try:
        invoice_id = _upload()
    finally:
        extractor_mod._service = old
    assert client.get(f"/api/invoices/{invoice_id}").json()["status"] == "on_hold"

    extractor_mod._service = ExtractionService(
        client=FakeClient([_payload_for(case)])
    )
    try:
        resp = client.post(f"/api/invoices/{invoice_id}/retry-extraction")
        assert resp.status_code == 202, resp.text
        # The 202 returns the reset row (received, error cleared) — the
        # background re-run then finishes before the next request.
        assert resp.json()["status"] == "received"
        assert resp.json()["extraction_error"] is None
        got = client.get(f"/api/invoices/{invoice_id}").json()
        assert got["status"] == "posted", got
        assert got["extraction_error"] is None
        assert got["extraction_error_kind"] is None
        assert got["invoice_number"] == case["invoice"].invoice_number
    finally:
        extractor_mod._service = old


def test_retry_extraction_refuses_non_retryable_status():
    case = next(c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == "perfect")
    _seed_po(case)
    old = extractor_mod._service
    extractor_mod._service = ExtractionService(client=FakeClient([_payload_for(case)]))
    try:
        invoice_id = _upload()
    finally:
        extractor_mod._service = old
    assert client.get(f"/api/invoices/{invoice_id}").json()["status"] == "posted"
    resp = client.post(f"/api/invoices/{invoice_id}/retry-extraction")
    assert resp.status_code == 409


def test_retry_extraction_422_when_document_bytes_gone():
    """After a server restart the in-process doc registry is empty: the
    retry must say so plainly (422) instead of re-running on nothing."""
    invoice_id = _upload()
    assert client.get(f"/api/invoices/{invoice_id}").json()["status"] == "on_hold"
    docstore.drop(invoice_id)
    resp = client.post(f"/api/invoices/{invoice_id}/retry-extraction")
    assert resp.status_code == 422
    assert "re-upload" in resp.json()["detail"]


def test_retry_extraction_cross_user_is_404():
    invoice_id = _upload()
    other = make_client(email="other@elric.dev")
    resp = other.post(f"/api/invoices/{invoice_id}/retry-extraction")
    assert resp.status_code == 404


def test_vendor_registry_lists_registered_vendor_with_no_invoices():
    """The FTUE case: a vendor added in onboarding has no invoices yet —
    the registry list must still show it (the Vendors page merges this
    with the invoice-derived summaries)."""
    resp = client.post(
        "/api/vendors",
        json={"name": "Onboarded Supplies Ltd", "gstin": "27AAAAA0000A1Z5", "state": "Maharashtra"},
    )
    assert resp.status_code == 201, resp.text
    vendor_id = resp.json()["id"]
    listed = client.get("/api/vendors/registry").json()
    assert [v["id"] for v in listed] == [vendor_id]
    assert listed[0]["name"] == "Onboarded Supplies Ltd"
    # Summaries are invoice-derived only: a vendor with no invoices
    # has no open balance row there.
    assert client.get("/api/vendors").json() == []


def test_vendor_registry_is_per_user():
    client.post("/api/vendors", json={"name": "My Vendor"})
    other = make_client(email="other2@elric.dev")
    assert other.get("/api/vendors/registry").json() == []
    # ...and the other user cannot fetch the vendor by id either.
    mine = client.get("/api/vendors/registry").json()[0]
    assert other.get(f"/api/vendors/{mine['id']}").status_code == 404


def test_registry_route_is_not_swallowed_by_vendor_id_route():
    """Regression: GET /api/vendors/registry must hit the list endpoint,
    not GET /api/vendors/{vendor_id} with id='registry'."""
    resp = client.get("/api/vendors/registry")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
