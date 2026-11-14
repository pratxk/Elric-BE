"""Extractor node — thin adapter over ExtractionService.

- If ``extracted`` is already seeded in state (tests / eval runs), it is
  passed through untouched — the real extractor is never called then.
- Otherwise the document bytes are read from the doc registry (filled at
  upload time) and extracted via the service: structured Gemini output,
  per-field confidence, arithmetic self-check with one re-extract, then
  escalate. On failure the invoice is parked on_hold — never guessed,
  never faked.
- The service is module-level and injectable: tests replace
  ``app.agents.extractor._service`` with a fake (no real Gemini calls).
"""

from __future__ import annotations

import re

from app.agents.state import InvoiceState
from app.domain.models import Vendor
from app.infra import docstore
from app.infra.gemini_client import GeminiConfigurationError, GeminiTransientError
from app.infra.store import store
from app.services.extraction_service import ExtractionService
from app.services.vendor_service import VendorService

_service = ExtractionService()


def _safe_message(message: str) -> str:
    """User-facing error text: truncated, and with any key material
    stripped (defence in depth — the client already sanitizes its own
    errors, but a transport error could still carry a request URL)."""
    text = re.sub(r"key=[^&\s]*", "key=[redacted]", str(message))
    return text[:300]


def classify_extraction_error(exc: Exception) -> tuple[str, str]:
    """(kind, message) for a failed extraction. The kind is what the UI
    keys its copy on — it must only ever be "no_key" when the backend
    genuinely has no key configured, so the frontend never again tells
    the user the key is missing when the real failure was a 503."""
    if isinstance(exc, GeminiConfigurationError):
        return (
            "no_key",
            "No Gemini API key is configured on this backend — set "
            "GEMINI_API_KEY and retry the invoice.",
        )
    if isinstance(exc, GeminiTransientError):
        if exc.status == 503:
            return ("overloaded", _safe_message(str(exc)))
        if exc.status == 429:
            return ("quota", _safe_message(str(exc)))
        return ("failed", _safe_message(str(exc)))
    return ("failed", _safe_message(str(exc)))


def _resolve_vendor_id(
    vendor_name: str, fallback: str, user_id: str | None = None
) -> str:
    """Fuzzy-match an extracted vendor name against the registry built
    from the USER's stored POs, invoices, and vendor registry. Returns
    the registry id on a match, else the fallback (slug) id. Scoped:
    another user's vendors never resolve here."""
    registry: list[Vendor] = []
    seen: set[str] = set()
    for po in store.list_pos(user_id=user_id):
        if po.vendor_id and po.vendor_name and po.vendor_id not in seen:
            seen.add(po.vendor_id)
            registry.append(Vendor(id=po.vendor_id, name=po.vendor_name))
    for row in store.list_invoices(user_id=user_id):
        if row.vendor_id and row.vendor_name and row.vendor_id not in seen:
            seen.add(row.vendor_id)
            registry.append(Vendor(id=row.vendor_id, name=row.vendor_name))
    if user_id is not None:
        for v in store.list_vendors(user_id):
            if v.id not in seen:
                seen.add(v.id)
                registry.append(Vendor(id=v.id, name=v.name))
    vendor, _score, _needs_confirm = VendorService(registry).find(vendor_name)
    return vendor.id if vendor is not None else fallback


def set_extraction_service(service: ExtractionService) -> None:
    """Test seam: swap the extraction service (e.g. a fake client)."""
    global _service
    _service = service


def extractor_node(state: InvoiceState) -> dict:
    if state.get("extracted"):
        return {
            "status": "extracted",
            "extraction_error": None,
            "extraction_error_kind": None,
            "audit": ["extractor: seeded extraction used"],
        }
    document_bytes = docstore.get(state.get("invoice_id", ""))
    if document_bytes is None:
        return {
            "extraction_error": "no document bytes registered for this invoice",
            "extraction_error_kind": "failed",
            "status": "on_hold",
            "audit": ["extractor: failed — no document bytes registered"],
        }
    try:
        extracted = _service.extract(document_bytes, "application/pdf")
    except Exception as exc:  # noqa: BLE001 — surface the failure, don't crash
        kind, message = classify_extraction_error(exc)
        return {
            "extraction_error": message,
            "extraction_error_kind": kind,
            "status": "on_hold",
            "audit": [f"extractor: failed — {message}"],
        }
    invoice = extracted.invoice
    # Identity: the extracted invoice IS the uploaded row. Pin its id to
    # the upload's invoice id so the ledger, payments and duplicate checks
    # all key on the same id (a fresh UUID here would orphan postings).
    invoice = invoice.model_copy(
        update={"id": state.get("invoice_id") or invoice.id}
    )
    # Vendor resolution: the extracted vendor id is a slug of the name,
    # but POs / prior invoices key on registry ids. Fuzzy-match the name
    # against vendors known from stored POs and invoices and adopt the
    # registry id when it clears the threshold; otherwise keep the slug
    # (new vendor → human-confirm path, v1 keeps the slug id).
    vendor_name = str(extracted.raw.get("vendor_name") or "")
    if vendor_name:
        resolved = _resolve_vendor_id(
            vendor_name, invoice.vendor_id, user_id=state.get("user_id")
        )
        if resolved != invoice.vendor_id:
            invoice = invoice.model_copy(update={"vendor_id": resolved})
    return {
        "extracted": {
            "invoice": invoice.model_dump(mode="json"),
            "po": state.get("po"),
            "gr": state.get("gr"),
            "existing_invoices": state.get("existing_invoices", []),
            "buyer_state": None,
            "seller_state": None,
            "field_confidences": extracted.field_confidences,
        },
        "vendor_name": str(extracted.raw.get("vendor_name") or ""),
        "invoice_number": invoice.invoice_number,
        "extraction_attempts": extracted.attempts,
        # Success clears any previous failure (a retry must not leave a
        # stale error on the invoice).
        "extraction_error": None,
        "extraction_error_kind": None,
        "status": "extracted",
        "audit": [
            f"extractor: fields extracted in {extracted.attempts} attempt(s); "
            f"confidence={invoice.extraction_confidence}"
        ],
    }
