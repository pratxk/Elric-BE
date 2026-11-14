"""API schemas — Pydantic v2 request/response models (§19g)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class InvoiceOut(BaseModel):
    id: str
    vendor_id: str
    vendor_name: str
    invoice_number: str
    invoice_date: str
    due_date: str | None = None
    currency: str = "INR"
    status: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    document_hash: str | None = None
    lines: list[dict] = Field(default_factory=list)
    match: dict | None = None
    exceptions: list[dict] = Field(default_factory=list)
    payment_terms: str | None = None


class DecisionIn(BaseModel):
    approver: str = "ap_clerk"
    reason: str = ""


class VendorQueryOut(BaseModel):
    invoice_id: str
    draft: str
    sent: bool = False  # drafts only — never auto-sent


class MetricsOut(BaseModel):
    total_invoices: int
    by_status: dict[str, int]
    exceptions_open: int
    money_in_exceptions_inr: Decimal
    auto_approved: int
    # Real numbers from the store / last eval run (0 / None when no data).
    match_rate: float = 0.0
    auto_approve_rate: float = 0.0
    false_approve_rate: float | None = None
    median_cycle_time_seconds: float | None = None
    overdue_count: int = 0
    overdue_total_inr: Decimal = Decimal("0.00")
    # Full aging breakdown (buckets + due-soon) so the dashboard needs
    # no second call for the exceptions card.
    aging_buckets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    due_soon_count: int = 0
    due_soon_total_inr: Decimal = Decimal("0.00")


class EvalRunOut(BaseModel):
    kind: str
    metrics: dict[str, Any]
    graph_eval: dict[str, Any] | None = None


class AgingOut(BaseModel):
    as_of: str
    buckets: dict[str, dict[str, Any]]
    overdue_count: int
    overdue_total_inr: Decimal
    oldest_overdue_days: int


class DiscountIn(BaseModel):
    invoice_id: str
    terms: str  # e.g. "2/10 Net 30"


class PurchaseOrderIn(BaseModel):
    """PO creation body: the domain PurchaseOrder plus the vendor's
    display name (used by the matcher to look the PO up by vendor)."""

    po_number: str
    vendor_id: str
    vendor_name: str = ""
    po_date: str
    total: Decimal = Decimal("0.00")
    lines: list[dict] = Field(default_factory=list)


class LedgerOut(BaseModel):
    id: str
    invoice_id: str
    account: str
    debit: Decimal
    credit: Decimal
    posted_at: str


class PaymentOut(BaseModel):
    id: str
    invoice_id: str
    amount: Decimal
    scheduled_date: str
    status: str
