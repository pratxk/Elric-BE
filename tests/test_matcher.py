"""Tests for the deterministic matcher + property tests (§17).

Property tests:
- a perfect invoice always auto-approves
- a duplicate never auto-approves
"""

from app.domain.models import MatchOutcome
from app.domain.policies import AutoApprovePolicy, auto_approve_decision
from app.eval.synthetic import generate_corpus
from app.services.matching_service import MatchingService


def _case(scenario: str):
    return next(c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == scenario)


def _match(case):
    return MatchingService().match(
        case["invoice"],
        case["po"],
        case["gr"],
        existing_invoices=case["existing"],
        buyer_state_code=case["buyer_state"],
        seller_state_code=case["seller_state"],
    )


def test_perfect_invoice_is_clean():
    match = _match(_case("perfect"))
    assert match.outcome == MatchOutcome.CLEAN
    assert match.exception_codes == []


def test_perfect_invoice_always_auto_approves():
    # Property: a perfect invoice always auto-approves (across seeds).
    policy = AutoApprovePolicy()
    for case in generate_corpus(n_per_scenario=3):
        if case["scenario"] != "perfect":
            continue
        match = _match(case)
        approved, _ = auto_approve_decision(match, case["invoice"].total, policy)
        assert approved is True


def test_duplicate_never_auto_approves():
    # Property: a duplicate never auto-approves (across seeds).
    policy = AutoApprovePolicy()
    for case in generate_corpus(n_per_scenario=3):
        if case["scenario"] != "duplicate":
            continue
        match = _match(case)
        assert match.outcome == MatchOutcome.DUPLICATE
        approved, _ = auto_approve_decision(match, case["invoice"].total, policy)
        assert approved is False


def test_price_variance_detected():
    match = _match(_case("price_variance"))
    assert "PRICE_VARIANCE" in [c.value for c in match.exception_codes]
    assert match.outcome == MatchOutcome.VARIANCE


def test_qty_short_detected():
    match = _match(_case("qty_short"))
    assert "QTY_SHORT" in [c.value for c in match.exception_codes]


def test_tax_mismatch_detected():
    match = _match(_case("tax_mismatch"))
    assert "TAX_MISMATCH" in [c.value for c in match.exception_codes]


def test_total_mismatch_detected():
    match = _match(_case("total_mismatch"))
    assert "TOTAL_MISMATCH" in [c.value for c in match.exception_codes]


def test_missing_po_detected():
    match = _match(_case("missing_po"))
    assert "MISSING_PO" in [c.value for c in match.exception_codes]
    assert match.outcome == MatchOutcome.MISSING_PO


def test_price_within_tolerance_passes():
    # Within ±2% or ₹500 (whichever larger): ₹100 bump on ₹65,000 passes.
    from decimal import Decimal

    from app.eval.synthetic import _base_po, _invoice_from_po

    po, gr = _base_po(1)
    inv = _invoice_from_po(po, 1, price_bump=Decimal("100"))
    match = MatchingService().match(
        inv, po, gr, buyer_state_code="29", seller_state_code="29"
    )
    assert match.outcome == MatchOutcome.CLEAN
