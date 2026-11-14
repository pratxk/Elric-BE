"""Domain entities — the Elric ontology (§19i).

Pure Pydantic v2 models. No FastAPI / LangChain / SQLAlchemy imports here.
Money is Decimal, never float. Models are frozen (immutable) where the
entity is a record of fact; mutable only where the lifecycle demands it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class InvoiceStatus(StrEnum):
    RECEIVED = "received"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    MATCHED = "matched"
    APPROVED = "approved"
    FLAGGED = "flagged"
    POSTED = "posted"
    SCHEDULED = "scheduled"
    PAID = "paid"
    # Side states
    DUPLICATE = "duplicate"
    ON_HOLD = "on_hold"
    REJECTED = "rejected"


class ExceptionCode(StrEnum):
    PRICE_VARIANCE = "PRICE_VARIANCE"
    QTY_SHORT = "QTY_SHORT"
    DUPLICATE = "DUPLICATE"
    MISSING_PO = "MISSING_PO"
    TAX_MISMATCH = "TAX_MISMATCH"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"


class MatchOutcome(StrEnum):
    CLEAN = "clean"
    VARIANCE = "variance"
    DUPLICATE = "duplicate"
    MISSING_PO = "missing_po"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Vendor(_Frozen):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    gstin: str | None = None
    state_code: str | None = None  # GST state code, e.g. "29" (Karnataka)
    bank_account_last4: str | None = None
    reliability_score: float | None = None  # 0..1, v1.1 vendor scoring


class InvoiceLine(_Frozen):
    description: str
    hsn_code: str | None = None
    qty: Decimal
    unit_price: Decimal
    # No default rate: a missing per-line rate means "unknown" (None),
    # NEVER 18%. Defaulting to Indian GST broke the arithmetic self-check
    # on every non-GST invoice (eval 2026-11-15: 3 of 4 US invoices).
    # Callers that need tax must check for None and skip that leg.
    tax_rate_pct: Decimal | None = None

    @property
    def line_subtotal(self) -> Decimal:
        from app.domain.money import to_money

        return to_money(self.qty * self.unit_price)

    @property
    def line_tax(self) -> Decimal | None:
        """Line tax, or None when the rate is unknown (no guessing)."""
        from app.domain.money import tax_amount

        if self.tax_rate_pct is None:
            return None
        return tax_amount(self.line_subtotal, self.tax_rate_pct)


class Invoice(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    vendor_id: str
    invoice_number: str
    invoice_date: date
    due_date: date | None = None
    currency: str = "INR"
    lines: list[InvoiceLine] = Field(default_factory=list)
    subtotal: Decimal = Decimal("0.00")
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    cgst: Decimal = Decimal("0.00")
    sgst: Decimal = Decimal("0.00")
    igst: Decimal = Decimal("0.00")
    status: InvoiceStatus = InvoiceStatus.RECEIVED
    document_hash: str | None = None
    extraction_confidence: float | None = None
    payment_terms: str | None = None  # e.g. "2/10 Net 30" — discount optimizer

    def recompute_totals(self) -> "Invoice":
        """Recompute subtotal/tax/total from lines. Returns a new instance.

        Tax is only recomputed when EVERY line carries a rate. When any
        rate is unknown, the stated tax_total is carried through and the
        total is subtotal + stated tax — the tax leg cannot be verified
        from lines, so it must not be invented (see the 18% default bug).
        """
        from app.domain.money import money_sum

        subtotal = money_sum([ln.line_subtotal for ln in self.lines])
        line_taxes = [ln.line_tax for ln in self.lines]
        if all(t is not None for t in line_taxes):
            tax = money_sum(line_taxes)  # type: ignore[arg-type]
        else:
            tax = self.tax_total
        return self.model_copy(
            update={"subtotal": subtotal, "tax_total": tax, "total": subtotal + tax}
        )

    @property
    def tax_rates_known(self) -> bool:
        """True when every line carries an explicit tax rate."""
        return all(ln.tax_rate_pct is not None for ln in self.lines)


class POLine(_Frozen):
    description: str
    hsn_code: str | None = None
    qty: Decimal
    unit_price: Decimal
    # Same rule as InvoiceLine: no 18% default. PO lines do not drive
    # tax math, but a missing rate must stay unknown, not become GST.
    tax_rate_pct: Decimal | None = None


class PurchaseOrder(_Frozen):
    id: str = Field(default_factory=lambda: str(uuid4()))
    po_number: str
    vendor_id: str
    po_date: date
    lines: list[POLine] = Field(default_factory=list)
    total: Decimal = Decimal("0.00")


class GRLine(_Frozen):
    description: str
    qty_received: Decimal
    qty_accepted: Decimal | None = None


class GoodsReceipt(_Frozen):
    id: str = Field(default_factory=lambda: str(uuid4()))
    gr_number: str
    po_id: str
    receipt_date: date
    lines: list[GRLine] = Field(default_factory=list)


class RuleOutcome(_Frozen):
    rule: str
    passed: bool
    detail: str = ""


class Match(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    invoice_id: str
    po_id: str | None = None
    gr_id: str | None = None
    outcome: MatchOutcome
    exception_codes: list[ExceptionCode] = Field(default_factory=list)
    rule_outcomes: list[RuleOutcome] = Field(default_factory=list)
    aligned_pairs: list[dict] = Field(default_factory=list)  # line alignments
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ExceptionRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    invoice_id: str
    code: ExceptionCode
    message: str
    severity: str = "medium"  # low | medium | high
    line_ref: str | None = None
    root_cause: str | None = None  # LLM analysis, v1
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Approval(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    invoice_id: str
    approver: str  # "auto" or a user id
    decision: str  # approved | rejected | info_requested
    reason: str = ""
    decided_at: datetime = Field(default_factory=datetime.utcnow)


class LedgerEntry(_Frozen):
    id: str = Field(default_factory=lambda: str(uuid4()))
    invoice_id: str
    account: str  # GL code, e.g. "AP-2000"
    debit: Decimal = Decimal("0.00")
    credit: Decimal = Decimal("0.00")
    posted_at: datetime = Field(default_factory=datetime.utcnow)


class Payment(_Frozen):
    id: str = Field(default_factory=lambda: str(uuid4()))
    invoice_id: str
    amount: Decimal
    scheduled_date: date
    status: str = "scheduled"  # scheduled | executed | failed
