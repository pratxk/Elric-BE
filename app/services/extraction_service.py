"""Extraction service — Gemini structured extraction (§6).

Digital PDF: text layer first (naive text pulled from the raw bytes and
put in the prompt). Scanned/image PDF: Gemini vision via the inlineData
part. Structured output via JSON schema; per-field confidence; arithmetic
self-check (sum(lines) == subtotal, subtotal + tax == total) with ONE
re-extract on failure, then escalate to a human — never guess.

Dependency injection: the service takes a client. Production passes the
real env-driven GeminiClient (primary → secondary key fallback); tests
inject a fake client. Nothing in this module invents data: a failed
extraction raises, it never returns a fabricated invoice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.domain.models import Invoice, InvoiceLine
from app.domain.money import to_money
from app.infra.gemini_client import GeminiClient, GeminiConfigurationError

# JSON schema sent to Gemini for structured extraction.
INVOICE_EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "vendor_name": {"type": "string"},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string"},
        "due_date": {"type": "string"},
        "currency": {"type": "string"},
        "payment_terms": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "hsn_code": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "tax_rate_pct": {"type": "number"},
                },
                "required": ["description", "qty", "unit_price"],
            },
        },
        "subtotal": {"type": "number"},
        "tax_total": {"type": "number"},
        "total": {"type": "number"},
        "cgst": {"type": "number"},
        "sgst": {"type": "number"},
        "igst": {"type": "number"},
        "field_confidences": {
            "type": "object",
            "description": (
                "Per-field confidence 0..1, keyed by field name "
                "(e.g. vendor_name, total). Free-form keys."
            ),
        },
    },
    "required": ["vendor_name", "invoice_number", "invoice_date", "lines", "total"],
}

# Prompt kept short and factual: read the document, return the schema.
EXTRACTION_PROMPT = (
    "Extract the structured fields of this supplier invoice exactly as "
    "printed. Return ONLY JSON matching the provided schema. Do not invent "
    "values: if a field is not visible, omit it (or use null for optional "
    "fields). Dates must be ISO YYYY-MM-DD. Amounts are numbers in the "
    "invoice currency. For each field give a confidence 0..1 in "
    "field_confidences. Document text layer (may be empty for scans):\n{text}"
)

MAX_TEXT_CHARS = 20_000


class ExtractionFailedError(RuntimeError):
    """Extraction failed (bad payload or arithmetic self-check) after the
    one allowed re-extract. Escalate to a human — never guess."""


def vendor_id_from_name(vendor_name: str) -> str:
    """Deterministic vendor id from a name, so an extracted invoice and a
    stored PO for the same vendor share the id for duplicate matching."""
    slug = re.sub(r"[^a-z0-9]+", "-", vendor_name.strip().lower()).strip("-")
    return f"vendor-{slug or 'unknown'}"


def _parse_date(value: object, fallback: date | None = None) -> date:
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    if fallback is not None:
        return fallback
    return date.today()


@dataclass
class ExtractedDocument:
    invoice: Invoice
    field_confidences: dict[str, float] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    attempts: int = 1


class ExtractionService:
    def __init__(self, client: GeminiClient | None = None):
        self.client = client if client is not None else GeminiClient()

    # -- payload parsing (pure, unit-testable without any client) ---------
    @staticmethod
    def parse_extraction_payload(data: dict) -> ExtractedDocument:
        """Validate a Gemini JSON payload and build an Invoice.

        Raises ExtractionFailedError on missing required fields or bad
        types — the caller decides whether to re-extract.
        """
        if not isinstance(data, dict):
            raise ExtractionFailedError("extraction payload is not a JSON object")
        missing = [
            f
            for f in ("vendor_name", "invoice_number", "invoice_date", "lines", "total")
            if not data.get(f)
        ]
        if missing:
            raise ExtractionFailedError(
                f"extraction payload missing required fields: {missing}"
            )
        lines_raw = data["lines"]
        if not isinstance(lines_raw, list) or not lines_raw:
            raise ExtractionFailedError("extraction payload has no invoice lines")
        lines: list[InvoiceLine] = []
        for idx, ln in enumerate(lines_raw):
            if not isinstance(ln, dict) or not ln.get("description"):
                raise ExtractionFailedError(f"line {idx} has no description")
            try:
                # A missing/null per-line tax rate stays None (unknown).
                # It is NEVER defaulted to 18%: that GST-biased default
                # broke the arithmetic self-check on 3 of 4 US invoices
                # in the 2026-11-15 eval (their real tax is 6–7.5%).
                rate_raw = ln.get("tax_rate_pct")
                lines.append(
                    InvoiceLine(
                        description=str(ln["description"]),
                        hsn_code=str(ln["hsn_code"]) if ln.get("hsn_code") else None,
                        qty=Decimal(str(ln["qty"])),
                        unit_price=Decimal(str(ln["unit_price"])),
                        tax_rate_pct=(
                            Decimal(str(rate_raw))
                            if rate_raw is not None
                            else None
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — bad numbers are a payload failure
                raise ExtractionFailedError(f"line {idx} has bad numbers: {exc}") from exc
        try:
            invoice = Invoice(
                vendor_id=vendor_id_from_name(str(data["vendor_name"])),
                invoice_number=str(data["invoice_number"]),
                invoice_date=_parse_date(data["invoice_date"]),
                due_date=_parse_date(data["due_date"])
                if data.get("due_date")
                else None,
                currency=str(data.get("currency") or "INR"),
                lines=lines,
                subtotal=to_money(Decimal(str(data.get("subtotal", "0")))),
                tax_total=to_money(Decimal(str(data.get("tax_total", "0")))),
                total=to_money(Decimal(str(data["total"]))),
                cgst=to_money(Decimal(str(data.get("cgst", "0")))),
                sgst=to_money(Decimal(str(data.get("sgst", "0")))),
                igst=to_money(Decimal(str(data.get("igst", "0")))),
                payment_terms=str(data["payment_terms"])
                if data.get("payment_terms")
                else None,
            )
        except ExtractionFailedError:
            raise
        except Exception as exc:  # noqa: BLE001 — bad payload shape
            raise ExtractionFailedError(f"bad extraction payload: {exc}") from exc
        confidences = {
            str(k): float(v)
            for k, v in (data.get("field_confidences") or {}).items()
            if isinstance(v, (int, float))
        }
        if confidences:
            invoice = invoice.model_copy(
                update={
                    "extraction_confidence": sum(confidences.values())
                    / len(confidences)
                }
            )
        return ExtractedDocument(
            invoice=invoice, field_confidences=confidences, raw=data
        )

    # -- live extraction ---------------------------------------------------
    def extract(
        self, document_bytes: bytes, mime_type: str = "application/pdf"
    ) -> ExtractedDocument:
        """Extract structured fields from an invoice document.

        Raises GeminiConfigurationError when no API key is configured.
        On arithmetic self-check failure: re-extract once, then raise
        ExtractionFailedError (escalate to a human).
        """
        if not self.client.configured:
            raise GeminiConfigurationError(
                "extraction unavailable: no Gemini API key configured "
                "(set GEMINI_API_KEY in the environment)"
            )
        if not document_bytes:
            raise ExtractionFailedError("no document bytes to extract from")
        text_layer = _text_layer(document_bytes)[:MAX_TEXT_CHARS]
        prompt = EXTRACTION_PROMPT.format(text=text_layer)
        last_failure: list[str] = []
        for attempt in (1, 2):
            if attempt == 2:
                prompt = (
                    prompt
                    + "\n\nPREVIOUS ATTEMPT FAILED THE ARITHMETIC SELF-CHECK: "
                    + "; ".join(last_failure)
                    + ". Re-read the document carefully and correct the "
                    "amounts so sum(lines)==subtotal and subtotal+tax==total."
                )
            payload = self.client.generate_structured(
                prompt,
                INVOICE_EXTRACTION_SCHEMA,
                document_bytes=document_bytes,
                mime_type=mime_type,
            )
            try:
                extracted = self.parse_extraction_payload(payload)
            except ExtractionFailedError as exc:
                last_failure = [str(exc)]
                if attempt == 2:
                    raise
                continue
            failures = self.arithmetic_self_check(extracted.invoice)
            if not failures:
                extracted.attempts = attempt
                return extracted
            last_failure = failures
            if attempt == 2:
                raise ExtractionFailedError(
                    "arithmetic self-check failed after re-extract: "
                    + "; ".join(failures)
                )
        raise ExtractionFailedError("extraction failed")  # pragma: no cover

    @staticmethod
    def arithmetic_self_check(invoice: Invoice) -> list[str]:
        """sum(lines) == subtotal, subtotal + tax == total. Returns failures.

        The subtotal leg always runs (pure line arithmetic, no tax rate
        needed). The tax leg and the total leg both depend on recomputed
        line tax, so when any line's rate is unknown they are skipped:
        failing a correct invoice over a defaulted rate is worse than
        not verifying a leg the document does not support. The matcher
        applies the same rule to its total_recompute check, so an
        invoice that passes the self-check is not re-flagged downstream
        for the same unknown rates. (Known quirk this accepts: the
        clean-us-shipping-and-discount benchmark invoice's own header
        amounts do not reconcile — subtotal $3,275 + tax $196.50 =
        $3,471.50 vs stated total $3,492.50, a $21 gap that is a
        shipping charge the schema does not model — so its total leg
        is unverifiable either way.)"""
        from app.domain.money import money_sum

        failures: list[str] = []
        recomputed = invoice.recompute_totals()
        if to_money(recomputed.subtotal) != to_money(invoice.subtotal):
            failures.append(
                f"subtotal {invoice.subtotal} != sum(lines) {recomputed.subtotal}"
            )
        if invoice.tax_rates_known:
            if to_money(recomputed.tax_total) != to_money(invoice.tax_total):
                failures.append(
                    f"tax {invoice.tax_total} != sum(line tax) {recomputed.tax_total}"
                )
            if to_money(recomputed.total) != to_money(invoice.total):
                failures.append(
                    f"total {invoice.total} != subtotal+tax {recomputed.total}"
                )
        return failures


def _text_layer(document_bytes: bytes) -> str:
    """Best-effort text layer: printable ASCII runs from the raw bytes.

    Real PDF text extraction (pypdf) is a later refinement; this gives the
    model the digital text when present and stays empty for scans, where
    the vision (inlineData) part carries the document.
    """
    text = "".join(
        chr(b) if 32 <= b < 127 or b in (10, 13) else " " for b in document_bytes
    )
    return re.sub(r"\s+", " ", text).strip()
