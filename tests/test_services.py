"""Service tests: root-cause (template + fake LLM), bookkeeping,
aging buckets, discount-terms parsing + cash-flow forecast.

No real Gemini calls anywhere: the LLM path is tested with a fake client.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from app.domain.models import Invoice, Match, MatchOutcome
from app.eval.synthetic import generate_corpus
from app.services.aging_service import compute_aging
from app.services.bookkeeping_service import BookkeepingService
from app.services.forecast_service import ForecastService, parse_payment_terms
from app.services.root_cause import RootCauseService


def _case(scenario="price_variance"):
    return next(
        c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == scenario
    )


def _match_for(case):
    from app.services.matching_service import MatchingService

    return MatchingService().match(
        case["invoice"],
        case["po"],
        case["gr"],
        existing_invoices=case["existing"],
        buyer_state_code=case["buyer_state"],
        seller_state_code=case["seller_state"],
    )


# -- root cause ------------------------------------------------------------


def test_root_cause_template_cites_evidence_and_fix():
    case = _case()
    match = _match_for(case)
    exceptions = [{"code": "PRICE_VARIANCE", "message": "line 0: price 70000 vs PO 65000"}]
    causes = RootCauseService(client=None).analyze(case["invoice"], match, exceptions)
    assert len(causes) == 1
    cause = causes[0]
    assert cause["code"] == "PRICE_VARIANCE"
    assert cause["root_cause"]
    assert cause["recommended_fix"]
    assert any("70000" in ev or "price_match" in ev for ev in cause["evidence"])


def test_root_cause_all_six_codes_have_templates():
    case = _case("perfect")
    exceptions = [
        {"code": code, "message": f"test {code}"}
        for code in (
            "PRICE_VARIANCE", "QTY_SHORT", "DUPLICATE",
            "MISSING_PO", "TAX_MISMATCH", "TOTAL_MISMATCH",
        )
    ]
    causes = RootCauseService(client=None).analyze(case["invoice"], None, exceptions)
    assert len(causes) == 6
    assert all(c["root_cause"] and c["recommended_fix"] for c in causes)


class FakeLLMClient:
    configured = True

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def generate_text(self, prompt):
        self.calls += 1
        return self.reply


def test_root_cause_llm_path_uses_client_and_validates_codes():
    case = _case()
    match = _match_for(case)
    reply = json.dumps(
        [
            {
                "code": "PRICE_VARIANCE",
                "root_cause": "Vendor raised laptop price without a PO revision.",
                "evidence": ["line 0: price 70000 vs PO 65000"],
                "recommended_fix": "Ask vendor for a credit note.",
            }
        ]
    )
    client = FakeLLMClient(reply)
    causes = RootCauseService(client=client).analyze(
        case["invoice"], match, [{"code": "PRICE_VARIANCE", "message": "x"}]
    )
    assert client.calls == 1
    assert causes[0]["root_cause"].startswith("Vendor raised")


def test_root_cause_llm_failure_falls_back_to_template():
    case = _case()
    client = FakeLLMClient("not json at all {{{")
    causes = RootCauseService(client=client).analyze(
        case["invoice"], None, [{"code": "PRICE_VARIANCE", "message": "x"}]
    )
    assert causes[0]["root_cause"]  # template fallback, queue never blocks


def test_vendor_query_draft_cites_amounts_and_is_marked_draft():
    case = _case()
    match = _match_for(case)
    causes = RootCauseService(client=None).analyze(
        case["invoice"], match, [{"code": "PRICE_VARIANCE", "message": "x"}]
    )
    draft = RootCauseService.draft_vendor_query(
        case["invoice"], causes, "Acme Corporation Pvt Ltd"
    )
    assert case["invoice"].invoice_number in draft
    assert "PRICE_VARIANCE" in draft
    assert "not sent" in draft.lower()


# -- bookkeeping -----------------------------------------------------------


def test_bookkeeping_posting_is_balanced_and_schedules_payment():
    case = _case("perfect")
    invoice = case["invoice"]
    plan = BookkeepingService().post_invoice(invoice, store=None)
    entries = plan["ledger_entries"]
    assert len(entries) == 2
    debits = sum(Decimal(e["debit"]) for e in entries)
    credits = sum(Decimal(e["credit"]) for e in entries)
    assert debits == credits == invoice.total
    assert {e["account"] for e in entries} == {"5000", "AP-2000"}
    assert plan["payment"]["status"] == "scheduled"
    assert plan["payment"]["amount"] == str(invoice.total)


def test_bookkeeping_discount_terms_pay_early_on_discount_date():
    case = _case("perfect")
    invoice = case["invoice"].model_copy(
        update={"payment_terms": "2/10 Net 30"}
    )
    plan = BookkeepingService().post_invoice(invoice, store=None)
    # 2% on ₹168,150 = ₹3,363 vs 20 days at 12% ≈ ₹1,106 → pay early.
    assert plan["discount"]["recommendation"] == "pay_early"
    assert plan["scheduled_date"] == plan["discount"]["discount_date"]
    assert plan["payment"]["scheduled_date"] == plan["discount"]["discount_date"]


def test_bookkeeping_no_terms_pays_on_due_date():
    case = _case("perfect")
    plan = BookkeepingService().post_invoice(case["invoice"], store=None)
    assert plan["scheduled_date"] == str(case["invoice"].due_date)


def test_bookkeeping_idempotent_second_post_is_noop():
    from app.infra.store import store

    case = _case("perfect")
    invoice = case["invoice"]
    # Persist once through the real store, then post again.
    first = BookkeepingService().post_invoice(invoice, store=store)
    assert first["already_posted"] is False
    second = BookkeepingService().post_invoice(invoice, store=store)
    assert second["already_posted"] is True
    assert len(store.ledger_for_invoice(invoice.id)) == 2


# -- aging -----------------------------------------------------------------


def _aging_invoice(due, total, status="flagged"):
    return {
        "due_date": due,
        "total": Decimal(total),
        "status": status,
    }


def test_aging_buckets():
    as_of = date(2026, 11, 15)
    invoices = [
        _aging_invoice(date(2026, 12, 1), "1000"),   # current
        _aging_invoice(date(2026, 11, 10), "2000"),  # 5 days overdue
        _aging_invoice(date(2026, 10, 1), "3000"),   # 45 days overdue
        _aging_invoice(date(2026, 8, 1), "4000"),    # 106 days overdue
        _aging_invoice(date(2026, 11, 1), "5000", status="paid"),  # excluded
        _aging_invoice(date(2026, 11, 1), "6000", status="rejected"),  # excluded
    ]
    result = compute_aging(invoices, as_of=as_of)
    buckets = result["buckets"]
    assert buckets["current"]["count"] == 1
    assert buckets["overdue_1_30"]["count"] == 1
    assert buckets["overdue_31_60"]["count"] == 1
    assert buckets["overdue_90_plus"]["count"] == 1
    assert result["overdue_count"] == 3
    assert result["overdue_total_inr"] == "9000.00"
    assert result["oldest_overdue_days"] == 106


# -- forecast --------------------------------------------------------------


def test_parse_payment_terms():
    assert parse_payment_terms("2/10 Net 30") == {
        "discount_pct": Decimal("2"),
        "discount_days": 10,
        "net_days": 30,
    }
    assert parse_payment_terms("1.5/15 Net 45")["discount_pct"] == Decimal("1.5")
    assert parse_payment_terms("Net 30") is None
    assert parse_payment_terms("") is None
    assert parse_payment_terms(None) is None


def test_discount_recommendation_npv_math():
    case = _case("perfect")
    invoice = case["invoice"]
    rec = ForecastService().discount_recommendation_for_terms(invoice, "2/10 Net 30")
    assert rec["recommendation"] == "pay_early"
    assert rec["discount_gained_inr"] == "3363.00"
    assert Decimal(rec["net_saving_inr"]) > 0
    # A tiny discount loses to the cost of capital → pay on due date.
    rec2 = ForecastService().discount_recommendation_for_terms(invoice, "0.5/10 Net 30")
    assert rec2["recommendation"] == "pay_on_due_date"


def test_weekly_outflows_flag_over_budget_weeks():
    case = _case("perfect")
    invoices = [case["invoice"]]
    start = date(2026, 11, 15)
    outflows = ForecastService().weekly_outflows(
        invoices, weeks=4, start=start, weekly_budget_inr=Decimal("100000")
    )
    # Due 2026-12-12 → week 3 (27 days out). Total 168,150 > 100,000 budget.
    assert outflows[3]["over_budget"] is True
    assert outflows[0]["over_budget"] is False
    forecast = ForecastService().forecast(
        invoices, weeks=4, start=start, weekly_budget_inr=Decimal("100000")
    )
    assert forecast["over_budget_weeks"] == [outflows[3]["week_start"]]
    assert forecast["total_projected_inr"] == "168150.00"
