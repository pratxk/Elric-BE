"""In-memory store for local dev.

Thin wrapper over the SQLAlchemy SQLite store — the API talks to this,
so swapping in a Postgres repository later is a one-file change.
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.infra import db
from app.infra.db import (
    AuditRow,
    EvalRunRow,
    GoodsReceiptRow,
    InvoiceRow,
    LedgerRow,
    PaymentRow,
    PurchaseOrderRow,
    SettingsRow,
    VendorRow,
)


class Store:
    def __init__(self) -> None:
        db.init_db()

    # -- invoices ---------------------------------------------------------
    # Every read/write below takes an optional user_id. When given, only
    # that user's rows are visible — legacy rows with user_id NULL are
    # never served (NULL != user_id). Internal callers (graph nodes,
    # eval) omit user_id and see everything; API routes always pass it.
    def create_invoice(self, record: dict) -> InvoiceRow:
        session = db.get_session()
        try:
            row = InvoiceRow(
                id=record["id"],
                user_id=record.get("user_id"),
                vendor_id=record.get("vendor_id", ""),
                vendor_name=record.get("vendor_name", ""),
                invoice_number=record.get("invoice_number", ""),
                invoice_date=record.get("invoice_date", ""),
                due_date=record.get("due_date"),
                currency=record.get("currency", "INR"),
                status=record.get("status", "received"),
                subtotal=Decimal(str(record.get("subtotal", "0"))),
                tax_total=Decimal(str(record.get("tax_total", "0"))),
                total=Decimal(str(record.get("total", "0"))),
                document_hash=record.get("document_hash"),
                extraction_error=record.get("extraction_error"),
                extraction_error_kind=record.get("extraction_error_kind"),
                lines_json=json.dumps(record.get("lines", []), default=str),
                match_json=json.dumps(record.get("match"), default=str)
                if record.get("match")
                else None,
                exceptions_json=json.dumps(record.get("exceptions", []), default=str),
                payment_terms=record.get("payment_terms"),
                extraction_json=json.dumps(record.get("extraction"), default=str)
                if record.get("extraction")
                else None,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        finally:
            session.close()

    def get_invoice(
        self, invoice_id: str, user_id: str | None = None
    ) -> InvoiceRow | None:
        session = db.get_session()
        try:
            row = session.get(InvoiceRow, invoice_id)
            if row is None:
                return None
            if user_id is not None and row.user_id != user_id:
                return None  # another user's row (or a legacy NULL row)
            return row
        finally:
            session.close()

    def list_invoices(
        self, status: str | None = None, user_id: str | None = None
    ) -> list[InvoiceRow]:
        session = db.get_session()
        try:
            q = session.query(InvoiceRow).order_by(InvoiceRow.created_at.desc())
            if status:
                q = q.filter(InvoiceRow.status == status)
            if user_id is not None:
                q = q.filter(InvoiceRow.user_id == user_id)
            return list(q.all())
        finally:
            session.close()

    def update_invoice(self, invoice_id: str, **fields) -> InvoiceRow | None:
        session = db.get_session()
        try:
            row = session.get(InvoiceRow, invoice_id)
            if row is None:
                return None
            for key, value in fields.items():
                if key in ("lines", "exceptions"):
                    setattr(row, f"{key}_json", json.dumps(value, default=str))
                elif key == "match":
                    row.match_json = json.dumps(value, default=str) if value else None
                elif key == "extraction":
                    row.extraction_json = (
                        json.dumps(value, default=str) if value else None
                    )
                elif key in ("subtotal", "tax_total", "total"):
                    setattr(row, key, Decimal(str(value)))
                else:
                    setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return row
        finally:
            session.close()

    # -- purchase orders / goods receipts --------------------------------
    def create_po(self, record: dict) -> PurchaseOrderRow:
        session = db.get_session()
        try:
            row = PurchaseOrderRow(
                id=record["id"],
                user_id=record.get("user_id"),
                po_number=record.get("po_number", ""),
                vendor_id=record.get("vendor_id", ""),
                vendor_name=record.get("vendor_name", ""),
                po_date=str(record.get("po_date", "")),
                total=Decimal(str(record.get("total", "0"))),
                lines_json=json.dumps(record.get("lines", []), default=str),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        finally:
            session.close()

    def list_pos(
        self, vendor_id: str | None = None, user_id: str | None = None
    ) -> list[PurchaseOrderRow]:
        session = db.get_session()
        try:
            q = session.query(PurchaseOrderRow).order_by(
                PurchaseOrderRow.created_at.desc()
            )
            if vendor_id:
                q = q.filter(PurchaseOrderRow.vendor_id == vendor_id)
            if user_id is not None:
                q = q.filter(PurchaseOrderRow.user_id == user_id)
            return list(q.all())
        finally:
            session.close()

    def get_po_by_vendor_name(
        self, vendor_name: str, user_id: str | None = None
    ) -> PurchaseOrderRow | None:
        """Latest PO whose vendor name matches (case-insensitive, trimmed)."""
        needle = (vendor_name or "").strip().lower()
        for row in self.list_pos(user_id=user_id):
            if row.vendor_name.strip().lower() == needle:
                return row
        return None

    def create_gr(self, record: dict) -> GoodsReceiptRow:
        session = db.get_session()
        try:
            row = GoodsReceiptRow(
                id=record["id"],
                user_id=record.get("user_id"),
                gr_number=record.get("gr_number", ""),
                po_id=record.get("po_id", ""),
                receipt_date=str(record.get("receipt_date", "")),
                lines_json=json.dumps(record.get("lines", []), default=str),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        finally:
            session.close()

    def get_gr_by_po(
        self, po_id: str, user_id: str | None = None
    ) -> GoodsReceiptRow | None:
        session = db.get_session()
        try:
            q = session.query(GoodsReceiptRow).filter(
                GoodsReceiptRow.po_id == po_id
            )
            if user_id is not None:
                q = q.filter(GoodsReceiptRow.user_id == user_id)
            return q.order_by(GoodsReceiptRow.created_at.desc()).first()
        finally:
            session.close()

    def list_grs(self, user_id: str | None = None) -> list[GoodsReceiptRow]:
        session = db.get_session()
        try:
            q = session.query(GoodsReceiptRow).order_by(
                GoodsReceiptRow.created_at.desc()
            )
            if user_id is not None:
                q = q.filter(GoodsReceiptRow.user_id == user_id)
            return list(q.all())
        finally:
            session.close()

    # -- ledger / payments -------------------------------------------------
    def save_ledger_entries(
        self, entries: list[dict], user_id: str | None = None
    ) -> list[LedgerRow]:
        session = db.get_session()
        try:
            rows = [
                LedgerRow(
                    id=e["id"],
                    user_id=user_id or e.get("user_id"),
                    invoice_id=e["invoice_id"],
                    account=e["account"],
                    debit=Decimal(str(e.get("debit", "0"))),
                    credit=Decimal(str(e.get("credit", "0"))),
                )
                for e in entries
            ]
            session.add_all(rows)
            session.commit()
            for r in rows:
                session.refresh(r)
            return rows
        finally:
            session.close()

    def ledger_for_invoice(self, invoice_id: str) -> list[LedgerRow]:
        session = db.get_session()
        try:
            return list(
                session.query(LedgerRow)
                .filter(LedgerRow.invoice_id == invoice_id)
                .order_by(LedgerRow.posted_at)
                .all()
            )
        finally:
            session.close()

    def list_ledger(self, user_id: str | None = None) -> list[LedgerRow]:
        session = db.get_session()
        try:
            q = session.query(LedgerRow).order_by(LedgerRow.posted_at.desc())
            if user_id is not None:
                q = q.filter(LedgerRow.user_id == user_id)
            return list(q.all())
        finally:
            session.close()

    def save_payment(
        self, record: dict, user_id: str | None = None
    ) -> PaymentRow:
        session = db.get_session()
        try:
            row = PaymentRow(
                id=record["id"],
                user_id=user_id or record.get("user_id"),
                invoice_id=record["invoice_id"],
                amount=Decimal(str(record.get("amount", "0"))),
                scheduled_date=str(record.get("scheduled_date", "")),
                status=record.get("status", "scheduled"),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        finally:
            session.close()

    def payments_for_invoice(self, invoice_id: str) -> list[PaymentRow]:
        session = db.get_session()
        try:
            return list(
                session.query(PaymentRow)
                .filter(PaymentRow.invoice_id == invoice_id)
                .order_by(PaymentRow.created_at)
                .all()
            )
        finally:
            session.close()

    def list_payments(self, user_id: str | None = None) -> list[PaymentRow]:
        session = db.get_session()
        try:
            q = session.query(PaymentRow).order_by(PaymentRow.created_at.desc())
            if user_id is not None:
                q = q.filter(PaymentRow.user_id == user_id)
            return list(q.all())
        finally:
            session.close()

    def vendor_balance(self, vendor_id: str) -> Decimal:
        """AP balance owed to a vendor: posted invoice totals minus
        executed payments. Scheduled payments don't reduce it yet."""
        total = Decimal("0.00")
        for row in self.list_invoices():
            if row.vendor_id == vendor_id and row.status in (
                "posted",
                "scheduled",
                "approved",
            ):
                total += Decimal(str(row.total))
        for pay in self.list_payments():
            if pay.status == "executed":
                inv = self.get_invoice(pay.invoice_id)
                if inv is not None and inv.vendor_id == vendor_id:
                    total -= Decimal(str(pay.amount))
        return total

    # -- audit ------------------------------------------------------------
    def record_audit(
        self,
        action: str,
        actor: str = "system",
        invoice_id: str | None = None,
        user_id: str | None = None,
        **detail,
    ) -> None:
        session = db.get_session()
        try:
            session.add(
                AuditRow(
                    user_id=user_id,
                    invoice_id=invoice_id,
                    actor=actor,
                    action=action,
                    detail_json=json.dumps(detail, default=str),
                )
            )
            session.commit()
        finally:
            session.close()

    def audit_for_invoice(self, invoice_id: str) -> list[AuditRow]:
        session = db.get_session()
        try:
            return list(
                session.query(AuditRow)
                .filter(AuditRow.invoice_id == invoice_id)
                .order_by(AuditRow.created_at)
                .all()
            )
        finally:
            session.close()

    # -- eval runs --------------------------------------------------------
    def save_eval_run(self, kind: str, metrics: dict) -> None:
        session = db.get_session()
        try:
            session.add(
                EvalRunRow(kind=kind, metrics_json=json.dumps(metrics, default=str))
            )
            session.commit()
        finally:
            session.close()

    def latest_eval_run(self, kind: str) -> EvalRunRow | None:
        session = db.get_session()
        try:
            return (
                session.query(EvalRunRow)
                .filter(EvalRunRow.kind == kind)
                .order_by(EvalRunRow.created_at.desc())
                .first()
            )
        finally:
            session.close()


    # -- settings (v1.1) ---------------------------------------------------
    def get_or_create_settings(self, user_id: str) -> SettingsRow:
        """The user's settings row, created with env-derived defaults on
        first read. The auto-approve default comes from
        AUTO_APPROVE_LIMIT_INR; the tolerance defaults are the §5 rules
        (±2% or ₹500) and the 90-day duplicate window."""
        from app.config import settings as app_settings

        session = db.get_session()
        try:
            row = session.get(SettingsRow, user_id)
            if row is None:
                row = SettingsRow(
                    user_id=user_id,
                    company_name=None,
                    country="IN",
                    state=None,
                    currency="INR",
                    auto_approve_limit_inr=Decimal(
                        str(app_settings.auto_approve_limit_inr)
                    ),
                    tolerance_pct=Decimal("2.0"),
                    tolerance_flat_inr=Decimal("500"),
                    duplicate_window_days=90,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
            return row
        finally:
            session.close()

    def update_settings(self, user_id: str, fields: dict) -> SettingsRow:
        row = self.get_or_create_settings(user_id)
        session = db.get_session()
        try:
            fresh = session.get(SettingsRow, user_id)
            for key, value in fields.items():
                if key in (
                    "auto_approve_limit_inr",
                    "tolerance_pct",
                    "tolerance_flat_inr",
                ):
                    setattr(fresh, key, Decimal(str(value)))
                elif key == "duplicate_window_days":
                    setattr(fresh, key, int(value))
                else:
                    setattr(fresh, key, value)
            session.commit()
            session.refresh(fresh)
            return fresh
        finally:
            session.close()

    def settings_dict(self, user_id: str) -> dict:
        """Plain-dict settings for the pipeline (graph state is JSON)."""
        row = self.get_or_create_settings(user_id)
        return {
            "company_name": row.company_name,
            "country": row.country,
            "state": row.state,
            "currency": row.currency,
            "auto_approve_limit_inr": str(row.auto_approve_limit_inr),
            "tolerance_pct": str(row.tolerance_pct),
            "tolerance_flat_inr": str(row.tolerance_flat_inr),
            "duplicate_window_days": row.duplicate_window_days,
        }

    # -- vendor registry (v1.1) --------------------------------------------
    # Never seeded, never global: every row belongs to one user, and
    # cross-user access returns None (→ 404 at the API layer).
    def create_vendor(self, user_id: str, record: dict) -> VendorRow:
        import uuid as _uuid

        session = db.get_session()
        try:
            row = VendorRow(
                id=str(_uuid.uuid4()),
                user_id=user_id,
                name=record["name"],
                gstin=record.get("gstin"),
                state=record.get("state"),
                payment_terms=record.get("payment_terms"),
                email=record.get("email"),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        finally:
            session.close()

    def get_vendor(
        self, vendor_id: str, user_id: str | None = None
    ) -> VendorRow | None:
        session = db.get_session()
        try:
            row = session.get(VendorRow, vendor_id)
            if row is None:
                return None
            if user_id is not None and row.user_id != user_id:
                return None
            return row
        finally:
            session.close()

    def list_vendors(self, user_id: str) -> list[VendorRow]:
        session = db.get_session()
        try:
            return list(
                session.query(VendorRow)
                .filter(VendorRow.user_id == user_id)
                .order_by(VendorRow.created_at.desc())
                .all()
            )
        finally:
            session.close()

    def update_vendor(
        self, vendor_id: str, user_id: str, fields: dict
    ) -> VendorRow | None:
        session = db.get_session()
        try:
            row = session.get(VendorRow, vendor_id)
            if row is None or row.user_id != user_id:
                return None
            for key, value in fields.items():
                if key in ("name", "gstin", "state", "payment_terms", "email"):
                    setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return row
        finally:
            session.close()

    def delete_vendor(self, vendor_id: str, user_id: str) -> bool:
        session = db.get_session()
        try:
            row = session.get(VendorRow, vendor_id)
            if row is None or row.user_id != user_id:
                return False
            session.delete(row)
            session.commit()
            return True
        finally:
            session.close()


store = Store()
