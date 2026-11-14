"""Matcher node — thin adapter over MatchingService (deterministic).

The service does all the math (Hungarian alignment, duplicate check, GST
validation, totals recompute). This node only resolves the inputs:

- PO / goods receipt: from the seeded state when present (tests / eval),
  otherwise looked up in the store by vendor name (PO) and PO id (GR).
- Existing invoices for the duplicate check: seeded ones plus every
  stored invoice for the same vendor.
- Each exception code gets its own message, citing only the failing
  rules that produced that code (not the whole failure list).
"""

from __future__ import annotations

from decimal import Decimal

from app.agents.state import InvoiceState
from app.domain.matching.tolerance import ToleranceConfig
from app.domain.models import (
    ExceptionCode,
    GoodsReceipt,
    Invoice,
    PurchaseOrder,
)
from app.domain.policies import auto_approve_decision, policy_from_settings
from app.infra.store import store
from app.services.matching_service import MatchingService

# Default (env/config) service, used when a run carries no per-user
# settings (eval, graph unit tests). Uploads always pass settings.
_service = MatchingService()


def _service_for_settings(settings: dict | None) -> tuple[MatchingService, int]:
    """Build the per-user matching service from the owner's settings.

    Returns (service, duplicate_window_days). Falls back to the
    env/config defaults for any missing key, so a run without settings
    behaves exactly as v1 did. The auto-approve limit is NOT read here
    — both the matcher and the approver gate take it from
    policy_from_settings() so the two can never disagree."""
    settings = settings or {}
    tolerance = ToleranceConfig(
        price_pct=Decimal(str(settings.get("tolerance_pct", "2.0"))) / Decimal("100"),
        price_abs=Decimal(str(settings.get("tolerance_flat_inr", "500"))),
    )
    window = int(settings.get("duplicate_window_days", 90))
    return MatchingService(tolerance=tolerance), window

# Which failing rules feed which exception code, for per-code messages.
_CODE_RULES: dict[ExceptionCode, set[str]] = {
    ExceptionCode.PRICE_VARIANCE: {"price_match"},
    ExceptionCode.QTY_SHORT: {"qty_match", "receipt_qty"},
    ExceptionCode.DUPLICATE: {"duplicate_check"},
    ExceptionCode.MISSING_PO: {"po_exists"},
    ExceptionCode.TAX_MISMATCH: {"tax_validation"},
    ExceptionCode.TOTAL_MISMATCH: {"total_recompute"},
}


def _row_to_invoice(row) -> Invoice | None:
    try:
        data = {
            "id": row.id,
            "vendor_id": row.vendor_id,
            "invoice_number": row.invoice_number,
            "invoice_date": row.invoice_date or "2026-01-01",
            "due_date": row.due_date,
            "currency": row.currency,
            "lines": row.lines(),
            "subtotal": str(row.subtotal),
            "tax_total": str(row.tax_total),
            "total": str(row.total),
            "status": row.status,
            "document_hash": row.document_hash,
        }
        return Invoice.model_validate(data)
    except Exception:  # noqa: BLE001 — a malformed stored row is skipped
        return None


def _resolve_po_gr(invoice: Invoice, extracted: dict, vendor_name: str = "", user_id: str | None = None):
    po = (
        PurchaseOrder.model_validate(extracted["po"])
        if extracted.get("po")
        else None
    )
    gr = (
        GoodsReceipt.model_validate(extracted["gr"])
        if extracted.get("gr")
        else None
    )
    if po is None:
        row = store.get_po_by_vendor_name(vendor_name, user_id=user_id) if vendor_name else None
        if row is not None:
            po = PurchaseOrder.model_validate(
                {
                    "id": row.id,
                    "po_number": row.po_number,
                    "vendor_id": row.vendor_id,
                    "po_date": row.po_date or "2026-01-01",
                    "lines": [
                        {
                            "description": ln.get("description", ""),
                            "hsn_code": ln.get("hsn_code"),
                            "qty": str(ln.get("qty", "0")),
                            "unit_price": str(ln.get("unit_price", "0")),
                            "tax_rate_pct": (
                                str(ln["tax_rate_pct"])
                                if ln.get("tax_rate_pct") is not None
                                else None
                            ),
                        }
                        for ln in row.lines()
                    ],
                    "total": str(row.total),
                }
            )
    if po is not None and gr is None:
        gr_row = store.get_gr_by_po(po.id, user_id=user_id)
        if gr_row is not None:
            gr = GoodsReceipt.model_validate(
                {
                    "id": gr_row.id,
                    "gr_number": gr_row.gr_number,
                    "po_id": gr_row.po_id,
                    "receipt_date": gr_row.receipt_date or "2026-01-01",
                    "lines": [
                        {
                            "description": ln.get("description", ""),
                            "qty_received": str(ln.get("qty_received", "0")),
                            "qty_accepted": ln.get("qty_accepted"),
                        }
                        for ln in gr_row.lines()
                    ],
                }
            )
    return po, gr


def matcher_node(state: InvoiceState) -> dict:
    extracted = state.get("extracted")
    if not extracted:
        return {
            "status": "flagged",
            "exceptions": [
                {"code": "MISSING_PO", "message": "no extracted invoice to match"}
            ],
            "audit": ["matcher: no extracted data — flagged"],
        }
    invoice = Invoice.model_validate(extracted["invoice"])
    user_id = state.get("user_id")
    po, gr = _resolve_po_gr(
        invoice,
        extracted,
        vendor_name=extracted.get("vendor_name")
        or state.get("vendor_name")
        or "",
        user_id=user_id,
    )
    existing = [
        Invoice.model_validate(i) for i in extracted.get("existing_invoices", [])
    ]
    # Duplicate check sees ONLY this user's prior invoices (legacy NULL
    # rows are invisible — list_invoices(user_id=...) excludes them).
    for row in store.list_invoices(user_id=user_id):
        if row.id == invoice.id or row.vendor_id != invoice.vendor_id:
            continue
        prior = _row_to_invoice(row)
        if prior is not None and all(p.id != prior.id for p in existing):
            existing.append(prior)
    service, window_days = _service_for_settings(state.get("settings"))
    match = service.match(
        invoice,
        po,
        gr,
        existing_invoices=existing,
        buyer_state_code=extracted.get("buyer_state"),
        seller_state_code=extracted.get("seller_state"),
        duplicate_window_days=window_days,
    )
    failing_by_rule: dict[str, list[str]] = {}
    for r in match.rule_outcomes:
        if not r.passed and r.detail:
            failing_by_rule.setdefault(r.rule, []).append(r.detail)
    exceptions = []
    for code in match.exception_codes:
        details = []
        for rule in _CODE_RULES.get(code, set()):
            details.extend(failing_by_rule.get(rule, []))
        if not details and code == ExceptionCode.DUPLICATE:
            details = failing_by_rule.get("duplicate_check", [])
        exceptions.append(
            {"code": code.value, "message": "; ".join(details) or code.value}
        )
    auto_approved, reason = auto_approve_decision(
        match, invoice.total, policy_from_settings(state.get("settings"))
    )
    return {
        "match": match.model_dump(mode="json"),
        "exceptions": exceptions,
        "status": "matched",
        "auto_approved": auto_approved,
        "audit": [
            f"matcher: outcome={match.outcome.value} "
            f"codes={sorted(c.value for c in match.exception_codes)} "
            f"auto_approve={auto_approved} ({reason})"
        ],
    }
