"""Approval service — human-in-the-loop decisions (§3, §19a.5).

Auto-approve only clean matches under the policy limit; everything else
goes to the queue. Every decision is recorded with actor + timestamp in
the audit log (append-only, via AuditService).
"""

from __future__ import annotations

from app.domain.lifecycle import transition
from app.domain.models import Approval, Invoice, InvoiceStatus
from app.domain.policies import AutoApprovePolicy, auto_approve_decision, route_approver


class ApprovalService:
    def __init__(self, policy: AutoApprovePolicy | None = None):
        self.policy = policy or AutoApprovePolicy()

    def try_auto_approve(self, invoice: Invoice, match_outcome: str, exception_codes: list) -> tuple[bool, str]:
        """Auto-approve path for the graph. Returns (approved, reason)."""
        from app.domain.models import Match, MatchOutcome

        match = Match(
            invoice_id=invoice.id,
            outcome=MatchOutcome(match_outcome),
            exception_codes=exception_codes,
        )
        return auto_approve_decision(match, invoice.total, self.policy)

    def approve(self, invoice: Invoice, approver: str, reason: str = "") -> tuple[Invoice, Approval]:
        new_status = transition(invoice.status, "approve")
        updated = invoice.model_copy(update={"status": new_status})
        approval = Approval(
            invoice_id=invoice.id, approver=approver, decision="approved", reason=reason
        )
        return updated, approval

    def reject(self, invoice: Invoice, approver: str, reason: str = "") -> tuple[Invoice, Approval]:
        new_status = transition(invoice.status, "reject")
        updated = invoice.model_copy(update={"status": new_status})
        approval = Approval(
            invoice_id=invoice.id, approver=approver, decision="rejected", reason=reason
        )
        return updated, approval

    def route(self, invoice: Invoice) -> str:
        """Approver role for the human queue, by amount tier."""
        return route_approver(invoice.total)
