"""Policies: tolerance config, auto-approve rules, approval routing.

Plain-English policies (§16b.3) compile to these rules in v1.1; for the
scaffold the rules are typed dataclasses with sensible defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.models import ExceptionCode, Match, MatchOutcome


@dataclass(frozen=True)
class AutoApprovePolicy:
    """A clean match auto-approves only when all of these hold."""

    max_amount_inr: Decimal = Decimal("500000")
    require_clean_match: bool = True
    max_vendor_variance_budget_used_pct: Decimal = Decimal("100")  # v1.1 budget
    blocked_exception_codes: frozenset[ExceptionCode] = frozenset(
        {
            ExceptionCode.DUPLICATE,
            ExceptionCode.MISSING_PO,
            ExceptionCode.TAX_MISMATCH,
            ExceptionCode.TOTAL_MISMATCH,
        }
    )


@dataclass(frozen=True)
class ApprovalTier:
    min_amount_inr: Decimal
    max_amount_inr: Decimal | None  # None = no ceiling
    approver_role: str


# Amount tiers → approver roles. Finance head signs off above ₹2L.
APPROVAL_TIERS: list[ApprovalTier] = [
    ApprovalTier(Decimal("0"), Decimal("50000"), "ap_clerk"),
    ApprovalTier(Decimal("50000"), Decimal("200000"), "ap_manager"),
    ApprovalTier(Decimal("200000"), None, "finance_head"),
]


def route_approver(amount_inr: Decimal) -> str:
    for tier in APPROVAL_TIERS:
        if amount_inr >= tier.min_amount_inr and (
            tier.max_amount_inr is None or amount_inr < tier.max_amount_inr
        ):
            return tier.approver_role
    return "finance_head"


def auto_approve_decision(
    match: Match, amount_inr: Decimal, policy: AutoApprovePolicy | None = None
) -> tuple[bool, str]:
    """Decide whether a matched invoice auto-approves. Returns (yes, reason)."""
    policy = policy or AutoApprovePolicy()
    if policy.require_clean_match and match.outcome != MatchOutcome.CLEAN:
        return False, f"match outcome is {match.outcome.value}, not clean"
    blocked = set(match.exception_codes) & set(policy.blocked_exception_codes)
    if blocked:
        return False, f"blocked exception codes: {sorted(c.value for c in blocked)}"
    if amount_inr > policy.max_amount_inr:
        return (
            False,
            f"amount ₹{amount_inr} exceeds auto-approve limit ₹{policy.max_amount_inr}",
        )
    return True, "clean match within auto-approve limit"
