"""API schemas — Pydantic v2 request/response models (§19g)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.services import auth_service as _auth


class UserOut(BaseModel):
    id: str
    email: str
    onboarding_completed: bool = False


class RegisterIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        return _auth.validate_email(v)

    @field_validator("password")
    @classmethod
    def _strong_password(cls, v: str) -> str:
        _auth.validate_password(v)
        return v


class LoginIn(BaseModel):
    email: str
    password: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        return _auth.validate_email(v)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _strong_password(cls, v: str) -> str:
        _auth.validate_password(v)
        return v


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class RefreshOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class SettingsOut(BaseModel):
    company_name: str | None = None
    country: str = "IN"
    state: str | None = None
    currency: str = "INR"
    auto_approve_limit_inr: Decimal
    tolerance_pct: Decimal
    tolerance_flat_inr: Decimal
    duplicate_window_days: int


class SettingsIn(BaseModel):
    company_name: str | None = None
    country: str = "IN"
    state: str | None = None
    currency: str = "INR"
    auto_approve_limit_inr: Decimal = Field(ge=0)
    tolerance_pct: Decimal = Field(ge=0, le=100)
    tolerance_flat_inr: Decimal = Field(ge=0)
    duplicate_window_days: int = Field(ge=1, le=365)


class VendorIn(BaseModel):
    name: str = Field(min_length=1)
    gstin: str | None = None
    state: str | None = None
    payment_terms: str | None = None
    email: str | None = None

    @field_validator("email")
    @classmethod
    def _valid_email_or_none(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return _auth.validate_email(v)


class VendorOut(BaseModel):
    id: str
    name: str
    gstin: str | None = None
    state: str | None = None
    payment_terms: str | None = None
    email: str | None = None


class EvalSummaryOut(BaseModel):
    synthetic: dict[str, Any] | None
    graph: dict[str, Any] | None
    generated_at: str
    cached: bool


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
    # Last extraction failure, persisted by the pipeline. The UI keys
    # its copy on extraction_error_kind — "no_key" only when the
    # backend genuinely has no key configured; "overloaded" (503),
    # "quota" (429) and "failed" carry the true reason in
    # extraction_error. Both are None when extraction succeeded or
    # has not run yet.
    extraction_error: str | None = None
    extraction_error_kind: str | None = None


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
    # Currency of money_in_exceptions_inr: the single currency of all
    # exception invoices, else "INR" (INR subset only) — never a sum
    # across currencies. Same rule for the overdue/due-soon totals.
    money_in_exceptions_currency: str = "INR"
    auto_approved: int
    # Real numbers from the store / last eval run (0 / None when no data).
    match_rate: float = 0.0
    auto_approve_rate: float = 0.0
    false_approve_rate: float | None = None
    median_cycle_time_seconds: float | None = None
    overdue_count: int = 0
    overdue_total_inr: Decimal = Decimal("0.00")
    overdue_total_currency: str = "INR"
    # Full aging breakdown (buckets + due-soon) so the dashboard needs
    # no second call for the exceptions card.
    aging_buckets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    due_soon_count: int = 0
    due_soon_total_inr: Decimal = Decimal("0.00")
    due_soon_total_currency: str = "INR"


class EvalRunOut(BaseModel):
    kind: str
    metrics: dict[str, Any]
    graph_eval: dict[str, Any] | None = None


class AgingOut(BaseModel):
    as_of: str
    buckets: dict[str, dict[str, Any]]
    overdue_count: int
    overdue_total_inr: Decimal
    overdue_total_currency: str = "INR"
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
