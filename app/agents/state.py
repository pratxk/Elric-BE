"""Shared graph state. TypedDict, validated at graph edges (§19g)."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class InvoiceState(TypedDict, total=False):
    invoice_id: str
    document_hash: str | None
    vendor_name: str | None
    invoice_number: str | None
    extracted: dict[str, Any] | None  # extracted fields + confidences
    extraction_error: str | None
    match: dict[str, Any] | None  # serialized Match
    exceptions: list[dict]
    status: str
    auto_approved: bool | None
    approval_decision: str | None  # resume value from interrupt()
    vendor_query_draft: str | None
    root_causes: list[dict]  # per-exception root-cause analysis
    extraction_attempts: int  # how many extraction calls were made (max 2)
    ledger_posted: bool  # bookkeeper persisted ledger entries
    payment_scheduled_date: str | None
    po: dict[str, Any] | None  # PO passthrough (tests / seeded runs)
    gr: dict[str, Any] | None  # goods-receipt passthrough
    existing_invoices: list[dict]  # prior invoices for the duplicate check
    audit: Annotated[list[str], operator.add]  # append-only trail of node events
