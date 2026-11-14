"""Supervisor node — owns the lifecycle state machine; routes, retries,
escalates. No business logic here beyond routing decisions."""

from __future__ import annotations

from app.agents.state import InvoiceState


def supervisor_node(state: InvoiceState) -> dict:
    return {"audit": ["supervisor: routing invoice " + state.get("invoice_id", "?")]}


def route_after_extract(state: InvoiceState) -> str:
    """Extraction failure (no key, bad payload, self-check after the one
    re-extract) parks the invoice on_hold and skips validate/match —
    nothing downstream may run on unextracted data."""
    if state.get("extraction_error"):
        return "audit"
    return "validate"


def route_after_match(state: InvoiceState) -> str:
    """Conditional edge: clean + auto-approved -> book; else exception path."""
    if state.get("auto_approved"):
        return "book"
    return "exception"


def route_after_approval(state: InvoiceState) -> str:
    decision = state.get("approval_decision")
    if decision == "approved":
        return "book"
    if decision == "rejected":
        return "done"
    return "exception"
