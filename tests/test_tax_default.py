"""Tax-default regression tests (eval finding 2026-11-15 §4.2).

The bug: ``parse_extraction_payload`` defaulted a missing per-line
``tax_rate_pct`` to 18 (Indian GST), so the arithmetic self-check
failed on 3 of 4 US invoices whose real tax is 6–7.5%. A missing rate
must stay unknown (None) — never 18 — and the tax-dependent legs of
the self-check are skipped when a rate is unknown.

ZERO LLM calls: these tests parse payloads and run the pure self-check.
The four already-extracted benchmark payloads in
``~/workspace/invoice-agent/eval-data/extracted/*.raw.json`` are
re-checked locally.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.models import InvoiceLine, POLine
from app.services.extraction_service import ExtractionService

EVAL_DATA = Path(__file__).resolve().parents[2] / "eval-data" / "extracted"

US_PAYLOAD = {
    "vendor_name": "Acme Industrial Supply Co",
    "invoice_number": "ACM-100145",
    "invoice_date": "2024-03-12",
    "currency": "USD",
    "lines": [
        {"description": "Bolts", "qty": 4, "unit_price": 48.50, "tax_rate_pct": None},
        {"description": "Washers", "qty": 2, "unit_price": 32.00},
        {"description": "Adhesive", "qty": 6, "unit_price": 14.75, "tax_rate_pct": None},
    ],
    "subtotal": 346.50,
    "tax_total": 25.99,
    "total": 372.49,
}


def test_missing_tax_rate_stays_none_not_18():
    doc = ExtractionService.parse_extraction_payload(US_PAYLOAD)
    rates = [ln.tax_rate_pct for ln in doc.invoice.lines]
    assert rates == [None, None, None]
    assert all(rate != Decimal("18") for rate in rates if rate is not None)


def test_no_model_default_is_18():
    # The domain defaults themselves must not be GST-biased: a line
    # built without a rate carries None, not 18.
    assert InvoiceLine(description="x", qty=Decimal("1"), unit_price=Decimal("1")).tax_rate_pct is None
    assert POLine(description="x", qty=Decimal("1"), unit_price=Decimal("1")).tax_rate_pct is None


def test_null_rate_payload_passes_self_check_when_headers_consistent():
    doc = ExtractionService.parse_extraction_payload(US_PAYLOAD)
    assert ExtractionService.arithmetic_self_check(doc.invoice) == []


def test_null_rate_payload_still_catches_subtotal_error():
    bad = json.loads(json.dumps(US_PAYLOAD))
    bad["subtotal"] = 300.00  # lines sum to 346.50
    doc = ExtractionService.parse_extraction_payload(bad)
    failures = ExtractionService.arithmetic_self_check(doc.invoice)
    assert any("subtotal" in f for f in failures)


def test_explicit_18_rate_still_parses_as_18():
    # The 18% value may still appear where an Indian invoice genuinely
    # states it — only the *default* is gone.
    payload = json.loads(json.dumps(US_PAYLOAD))
    payload["lines"][0]["tax_rate_pct"] = 18
    payload["tax_total"] = 62.37  # 18% of 346.50
    payload["total"] = 408.87
    doc = ExtractionService.parse_extraction_payload(payload)
    assert doc.invoice.lines[0].tax_rate_pct == Decimal("18")
    # …but the other two lines are still unknown, so the tax legs skip
    # and only the (correct) subtotal leg runs.
    assert ExtractionService.arithmetic_self_check(doc.invoice) == []


def test_known_rates_still_fail_on_wrong_tax():
    payload = json.loads(json.dumps(US_PAYLOAD))
    for ln in payload["lines"]:
        ln["tax_rate_pct"] = 18
    doc = ExtractionService.parse_extraction_payload(payload)
    failures = ExtractionService.arithmetic_self_check(doc.invoice)
    assert any(f.startswith("tax ") for f in failures)


@pytest.mark.parametrize(
    "doc_id",
    [
        "clean-us-clean-simple",
        "clean-gb-serif",
        "clean-us-two-tax-lines",
        "clean-us-shipping-and-discount",
    ],
)
def test_eval_extracted_payloads_pass_self_check(doc_id):
    """Re-run the self-check on the four successful live extractions
    from the 2026-11-15 eval (zero new LLM calls). Before the fix the
    three US ones failed on the defaulted 18%; after it all four pass
    (gb-serif passed before and still passes)."""
    path = EVAL_DATA / f"{doc_id}.raw.json"
    if not path.exists():
        pytest.skip(f"eval payload not present: {path}")
    raw = json.loads(path.read_text())
    doc = ExtractionService.parse_extraction_payload(raw)
    assert ExtractionService.arithmetic_self_check(doc.invoice) == []
