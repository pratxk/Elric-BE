"""Validate node — arithmetic self-check on extracted fields (§6).

sum(lines) == subtotal, subtotal + tax == total. On failure: flag for
human review. Never guess.
"""

from __future__ import annotations

from app.agents.state import InvoiceState
from app.domain.models import Invoice
from app.services.extraction_service import ExtractionService


def validate_node(state: InvoiceState) -> dict:
    extracted = state.get("extracted")
    if not extracted:
        return {
            "status": "flagged",
            "audit": ["validate: no extracted data — flagged"],
        }
    invoice = Invoice.model_validate(extracted["invoice"])
    failures = ExtractionService.arithmetic_self_check(invoice)
    if failures:
        return {
            "status": "flagged",
            "exceptions": [
                {"code": "TOTAL_MISMATCH", "message": f}
                for f in failures
            ],
            "audit": [f"validate: arithmetic self-check failed — {failures}"],
        }
    return {"status": "validated", "audit": ["validate: arithmetic self-check passed"]}
