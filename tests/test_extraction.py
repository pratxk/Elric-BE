"""Extraction service tests — mocked Gemini client only.

Quota rule: ZERO real Gemini calls. The fake client below records calls
and returns canned payloads; no key is configured anywhere in this file.
"""

import pytest
from decimal import Decimal

from app.eval.synthetic import generate_corpus
from app.infra.gemini_client import GeminiConfigurationError
from app.services.extraction_service import (
    ExtractionFailedError,
    ExtractionService,
    vendor_id_from_name,
)


def _perfect_payload():
    case = next(
        c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == "perfect"
    )
    inv = case["invoice"]
    return {
        "vendor_name": "Acme Corporation Pvt Ltd",
        "invoice_number": inv.invoice_number,
        "invoice_date": str(inv.invoice_date),
        "due_date": str(inv.due_date),
        "currency": "INR",
        "payment_terms": "2/10 Net 30",
        "lines": [
            {
                "description": ln.description,
                "hsn_code": ln.hsn_code,
                "qty": float(ln.qty),
                "unit_price": float(ln.unit_price),
                "tax_rate_pct": float(ln.tax_rate_pct),
            }
            for ln in inv.lines
        ],
        "subtotal": float(inv.subtotal),
        "tax_total": float(inv.tax_total),
        "total": float(inv.total),
        "cgst": float(inv.cgst),
        "sgst": float(inv.sgst),
        "igst": float(inv.igst),
        "field_confidences": {"total": 0.98, "vendor_name": 0.99},
    }


class FakeClient:
    """Fake Gemini client: returns canned payloads, counts calls."""

    def __init__(self, payloads, configured=True):
        self._payloads = list(payloads)
        self._configured = configured
        self.calls = 0
        self.last_prompt = ""

    @property
    def configured(self):
        return self._configured

    def generate_structured(self, prompt, schema, **kwargs):
        self.calls += 1
        self.last_prompt = prompt
        return self._payloads[min(self.calls - 1, len(self._payloads) - 1)]


def test_extract_valid_payload_returns_invoice():
    service = ExtractionService(client=FakeClient([_perfect_payload()]))
    extracted = service.extract(b"%PDF-fake", "application/pdf")
    assert extracted.attempts == 1
    assert extracted.invoice.invoice_number.startswith("INV-2026-")
    assert extracted.invoice.vendor_id == vendor_id_from_name(
        "Acme Corporation Pvt Ltd"
    )
    assert extracted.invoice.payment_terms == "2/10 Net 30"
    assert extracted.invoice.total == Decimal("168150.00")
    assert extracted.field_confidences["total"] == 0.98
    assert extracted.invoice.extraction_confidence is not None


def test_extract_arithmetic_failure_retries_once_then_succeeds():
    bad = _perfect_payload()
    bad["total"] = 999999.99  # fails sum(lines)==subtotal+tax
    service = ExtractionService(client=FakeClient([bad, _perfect_payload()]))
    extracted = service.extract(b"%PDF-fake")
    assert extracted.attempts == 2
    assert extracted.invoice.total == Decimal("168150.00")
    assert "self-check" in service.client.last_prompt.lower() or "SELF-CHECK" in (
        service.client.last_prompt
    )


def test_extract_arithmetic_failure_twice_escalates():
    bad = _perfect_payload()
    bad["total"] = 999999.99
    service = ExtractionService(client=FakeClient([bad, bad]))
    with pytest.raises(ExtractionFailedError, match="self-check"):
        service.extract(b"%PDF-fake")
    assert service.client.calls == 2  # exactly one re-extract, then escalate


def test_extract_missing_required_field_escalates_after_retry():
    payload = _perfect_payload()
    del payload["lines"]
    service = ExtractionService(client=FakeClient([payload, payload]))
    with pytest.raises(ExtractionFailedError):
        service.extract(b"%PDF-fake")


def test_extract_no_key_configured_raises_configuration_error():
    service = ExtractionService(client=FakeClient([], configured=False))
    with pytest.raises(GeminiConfigurationError):
        service.extract(b"%PDF-fake")
    assert service.client.calls == 0  # no call is ever attempted without a key


def test_extract_empty_document_raises_without_calling_client():
    service = ExtractionService(client=FakeClient([_perfect_payload()]))
    with pytest.raises(ExtractionFailedError):
        service.extract(b"")
    assert service.client.calls == 0


def test_arithmetic_self_check_detects_bad_totals():
    case = next(
        c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == "perfect"
    )
    inv = case["invoice"].model_copy(update={"total": Decimal("1.00")})
    failures = ExtractionService.arithmetic_self_check(inv)
    assert failures  # total mismatch flagged
    assert not ExtractionService.arithmetic_self_check(case["invoice"])


def test_parse_payload_rejects_bad_line_numbers():
    payload = _perfect_payload()
    payload["lines"][0]["qty"] = "not-a-number"
    with pytest.raises(ExtractionFailedError):
        ExtractionService.parse_extraction_payload(payload)
