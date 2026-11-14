"""Duplicate detection (§5, §16a.4).

- Exact: same vendor + invoice number.
- Near: same vendor + same amount + invoice date within a window.
- Paid check: same vendor + invoice number already paid (cross-check
  against paid invoices) — duplicate payment is the most expensive AP failure.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.models import Invoice
from app.domain.money import to_money

NEAR_DUPLICATE_WINDOW_DAYS = 90


def is_exact_duplicate(invoice: Invoice, existing: list[Invoice]) -> Invoice | None:
    for other in existing:
        if other.id == invoice.id:
            continue
        if (
            other.vendor_id == invoice.vendor_id
            and other.invoice_number.strip().lower()
            == invoice.invoice_number.strip().lower()
        ):
            return other
    return None


def is_near_duplicate(
    invoice: Invoice,
    existing: list[Invoice],
    window_days: int = NEAR_DUPLICATE_WINDOW_DAYS,
) -> Invoice | None:
    for other in existing:
        if other.id == invoice.id or other.vendor_id != invoice.vendor_id:
            continue
        same_amount = to_money(other.total) == to_money(invoice.total)
        within_window = abs((other.invoice_date - invoice.invoice_date).days) <= window_days
        if same_amount and within_window:
            return other
    return None


def is_already_paid(invoice: Invoice, paid_invoices: list[Invoice]) -> Invoice | None:
    """Cross-check against already-paid invoices (same vendor + number)."""
    for other in paid_invoices:
        if (
            other.vendor_id == invoice.vendor_id
            and other.invoice_number.strip().lower()
            == invoice.invoice_number.strip().lower()
        ):
            return other
    return None


def check_duplicate(
    invoice: Invoice, existing: list[Invoice], paid: list[Invoice] | None = None
) -> tuple[str | None, Invoice | None]:
    """Returns (kind, matched_invoice): kind in {"exact", "near", "paid"}."""
    hit = is_exact_duplicate(invoice, existing)
    if hit:
        return "exact", hit
    if paid:
        hit = is_already_paid(invoice, paid)
        if hit:
            return "paid", hit
    hit = is_near_duplicate(invoice, existing)
    if hit:
        return "near", hit
    return None, None
