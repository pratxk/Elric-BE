"""Shared graph state. TypedDict, validated at graph edges (§19g)."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class InvoiceState(TypedDict, total=False):
    invoice_id: str
    document_hash: str | None
    user_id: str | None  # v1.1: owner of the invoice; scopes all lookups
    # v1.1: the owner's settings snapshot at upload time, so the
    # pipeline uses the LOGGED-IN USER's limits/tolerances, not env
    # defaults: {auto_approve_limit_inr, tolerance_pct,
    # tolerance_flat_inr, duplicate_window_days, ...}
    settings: dict[str, Any] | None
    vendor_name: str | None
    invoice_number: str | None
    extracted: dict[str, Any] | None  # extracted fields + confidences
    extraction_error: str | None
    # Machine-readable failure kind: "no_key" | "overloaded" | "quota" |
    # "failed". The UI keys its copy on this — "no_key" only when the
    # backend genuinely has no key configured.
    extraction_error_kind: str | None
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
