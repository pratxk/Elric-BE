"""Bookkeeper service — posts approved invoices to the ledger (SQLite)
and schedules payment (§3).

Balanced double-entry per invoice: Dr purchases (GL 5000) / Cr accounts
payable (GL AP-2000), both equal to the invoice total. The payment date
is discount-optimized when payment terms parse (2/10 Net 30): pay on the
discount date when the NPV says pay early, otherwise on the due date.
Payments are SCHEDULED, never executed, without human approval (§19a.5).

``store`` is injected (the SQLite store in production / the graph node);
with ``store=None`` the service computes the entries without persisting,
so the pure math stays unit-testable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.domain.models import Invoice
from app.domain.money import to_money
from app.services.forecast_service import ForecastService

GL_PURCHASES = "5000"  # purchases / expense
GL_ACCOUNTS_PAYABLE = "AP-2000"


class BookkeepingService:
    def __init__(self, forecast: ForecastService | None = None):
        self.forecast = forecast or ForecastService()

    def plan_posting(
        self, invoice: Invoice, payment_terms: str | None = None
    ) -> dict:
        """Compute ledger entries + scheduled payment without persisting."""
        terms = payment_terms or invoice.payment_terms
        total = to_money(invoice.total)
        entries = [
            {
                "id": str(uuid4()),
                "invoice_id": invoice.id,
                "account": GL_PURCHASES,
                "debit": str(total),
                "credit": "0.00",
            },
            {
                "id": str(uuid4()),
                "invoice_id": invoice.id,
                "account": GL_ACCOUNTS_PAYABLE,
                "debit": "0.00",
                "credit": str(total),
            },
        ]
        pay_date = self._scheduled_date(invoice, terms)
        discount_info = None
        if terms:
            rec = self.forecast.discount_recommendation_for_terms(invoice, terms)
            if rec is not None:
                discount_info = rec
        payment = {
            "id": str(uuid4()),
            "invoice_id": invoice.id,
            "amount": str(total),
            "scheduled_date": str(pay_date),
            "status": "scheduled",
        }
        return {
            "ledger_entries": entries,
            "payment": payment,
            "scheduled_date": str(pay_date),
            "discount": discount_info,
        }

    def post_invoice(
        self, invoice: Invoice, payment_terms: str | None = None, store=None
    ) -> dict:
        """Post to the ledger and schedule payment; persist when a store
        is given. Idempotent per invoice: an already-posted invoice is
        not posted twice."""
        if store is not None and store.ledger_for_invoice(invoice.id):
            existing_payment = store.payments_for_invoice(invoice.id)
            return {
                "ledger_entries": [],
                "payment": existing_payment[0].__dict__ if existing_payment else None,
                "scheduled_date": existing_payment[0].scheduled_date
                if existing_payment
                else None,
                "discount": None,
                "already_posted": True,
            }
        plan = self.plan_posting(invoice, payment_terms)
        if store is not None:
            store.save_ledger_entries(plan["ledger_entries"])
            store.save_payment(plan["payment"])
        plan["already_posted"] = False
        return plan

    def _scheduled_date(
        self, invoice: Invoice, terms: str | None
    ) -> date:
        if terms:
            rec = self.forecast.discount_recommendation_for_terms(invoice, terms)
            if rec is not None:
                return date.fromisoformat(rec["pay_date"])
        if invoice.due_date:
            return invoice.due_date
        return invoice.invoice_date  # no due date known: schedule on invoice date
