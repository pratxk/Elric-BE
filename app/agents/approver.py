"""Approver node — human-in-the-loop via LangGraph interrupt().

Auto-approve path never reaches this node. When a human decision is
required, the graph pauses here until resumed with approve/reject/
info_requested. The resume value is the decision string:
"approved" | "rejected" | "info_requested".
"""

from __future__ import annotations

from decimal import Decimal

from langgraph.types import interrupt

from app.agents.state import InvoiceState
from app.domain.models import Match
from app.domain.policies import auto_approve_decision


def approver_node(state: InvoiceState) -> dict:
    match_data = state.get("match") or {}
    match = Match.model_validate(match_data) if match_data else None
    # Re-check the policy at the gate (defence in depth).
    if match is not None:
        total = Decimal(str((state.get("extracted") or {}).get("total", "0")))
        if not total:
            extracted = state.get("extracted") or {}
            inv = extracted.get("invoice") or {}
            total = Decimal(str(inv.get("total", "0")))
        approved, reason = auto_approve_decision(match, total)
        if approved:
            return {
                "auto_approved": True,
                "status": "approved",
                "audit": [f"approver: auto-approved — {reason}"],
            }
    decision = interrupt(
        {
            "invoice_id": state.get("invoice_id"),
            "exceptions": state.get("exceptions", []),
            "question": "Approve, reject, or request info for this invoice?",
        }
    )
    if decision == "approved":
        status = "approved"
    elif decision == "rejected":
        status = "rejected"
    else:  # "info_requested" or anything unexpected → back to the queue
        status = "flagged"
        decision = "info_requested"
    return {
        "approval_decision": decision,
        "status": status,
        "audit": [f"approver: human decision={decision}"],
    }
