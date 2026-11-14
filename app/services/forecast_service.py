"""Forecast service — early-payment discount optimizer + cash-flow forecast.

§16a.6/7: cheap deterministic math, big demo value. Real NPV math lives in
domain/money.py; this service applies it per invoice and aggregates
scheduled outflows by week.

Terms parsing: "2/10 Net 30" means 2% discount if paid within 10 days,
otherwise the net amount is due in 30 days. NPV vs cost of capital decides
pay-now vs pay-later per invoice.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from app.domain.models import Invoice
from app.domain.money import early_payment_discount_value, to_money

TERMS_RE = re.compile(
    r"(?P<discount_pct>\d+(?:\.\d+)?)\s*/\s*(?P<discount_days>\d+)\s+Net\s+(?P<net_days>\d+)",
    re.IGNORECASE,
)


def parse_payment_terms(terms: str | None) -> dict | None:
    """Parse "2/10 Net 30" → {discount_pct, discount_days, net_days}.

    Returns None when the string doesn't match the pattern (e.g. "Net 30"
    with no discount, or free text) — the caller then pays on the due date.
    """
    if not terms:
        return None
    m = TERMS_RE.search(terms.strip())
    if not m:
        return None
    return {
        "discount_pct": Decimal(m.group("discount_pct")),
        "discount_days": int(m.group("discount_days")),
        "net_days": int(m.group("net_days")),
    }


class ForecastService:
    def __init__(self, cost_of_capital_annual_pct: Decimal = Decimal("12")):
        self.cost_of_capital = cost_of_capital_annual_pct

    def discount_recommendation(
        self,
        invoice: Invoice,
        discount_pct: Decimal,
        days_early: int,
    ) -> dict:
        """Recommend pay-now vs pay-later for terms like 2/10 Net 30."""
        discount, opportunity_cost, pay_early = early_payment_discount_value(
            invoice.total, discount_pct, days_early, self.cost_of_capital
        )
        return {
            "invoice_id": invoice.id,
            "discount_pct": str(discount_pct),
            "discount_gained_inr": str(discount),
            "opportunity_cost_inr": str(opportunity_cost),
            "recommendation": "pay_early" if pay_early else "pay_on_due_date",
        }

    def discount_recommendation_for_terms(
        self, invoice: Invoice, terms: str | None
    ) -> dict | None:
        """Full per-invoice recommendation from a terms string.

        Returns None when terms don't parse. Otherwise includes the
        discount date, the net (due) date, the NPV numbers and the pay date
        to schedule: discount date when pay-early wins, else the due date.
        """
        parsed = parse_payment_terms(terms)
        if parsed is None:
            return None
        discount_days = parsed["discount_days"]
        net_days = parsed["net_days"]
        days_early = max(net_days - discount_days, 0)
        discount, opportunity_cost, pay_early = early_payment_discount_value(
            invoice.total,
            parsed["discount_pct"],
            days_early,
            self.cost_of_capital,
        )
        discount_date = invoice.invoice_date + timedelta(days=discount_days)
        net_date = invoice.invoice_date + timedelta(days=net_days)
        due = invoice.due_date or net_date
        return {
            "invoice_id": invoice.id,
            "terms": terms,
            "discount_pct": str(parsed["discount_pct"]),
            "discount_days": discount_days,
            "net_days": net_days,
            "discount_date": str(discount_date),
            "net_date": str(net_date),
            "discount_gained_inr": str(discount),
            "opportunity_cost_inr": str(opportunity_cost),
            "recommendation": "pay_early" if pay_early else "pay_on_due_date",
            "pay_date": str(discount_date if pay_early else due),
            "net_saving_inr": str(to_money(discount - opportunity_cost)),
        }

    def weekly_outflows(
        self,
        invoices: list[Invoice],
        weeks: int = 4,
        start: date | None = None,
        weekly_budget_inr: Decimal | None = None,
    ) -> list[dict]:
        """Projected outflows by week from due dates.

        Weeks are anchored at ``start`` (default today). Only open
        (unpaid) invoices count. Each week flags over-budget when a
        weekly budget is given. Invoices already overdue land in week 0.
        """
        start = start or date.today()
        buckets = [Decimal("0.00")] * weeks
        for inv in invoices:
            if getattr(inv, "status", None) in ("paid", "rejected", "duplicate"):
                continue
            due = inv.due_date or inv.invoice_date
            week = (due - start).days // 7
            if week < 0:
                week = 0
            if week < weeks:
                buckets[week] += to_money(inv.total)
        out = []
        for i, b in enumerate(buckets):
            entry: dict = {
                "week_start": str(start + timedelta(days=7 * i)),
                "projected_outflow_inr": str(to_money(b)),
            }
            if weekly_budget_inr is not None:
                entry["budget_inr"] = str(to_money(weekly_budget_inr))
                entry["over_budget"] = b > to_money(weekly_budget_inr)
            out.append(entry)
        return out

    def forecast(
        self,
        invoices: list[Invoice],
        weeks: int = 4,
        start: date | None = None,
        weekly_budget_inr: Decimal | None = None,
    ) -> dict:
        """Cash-flow forecast: weekly outflows + per-invoice discount
        recommendations (only for invoices whose terms parse)."""
        outflows = self.weekly_outflows(invoices, weeks, start, weekly_budget_inr)
        recommendations = []
        for inv in invoices:
            if getattr(inv, "status", None) in ("paid", "rejected", "duplicate"):
                continue
            rec = self.discount_recommendation_for_terms(inv, inv.payment_terms)
            if rec is not None:
                recommendations.append(rec)
        total_projected = sum(
            (Decimal(o["projected_outflow_inr"]) for o in outflows), Decimal("0.00")
        )
        over_budget_weeks = [
            o["week_start"] for o in outflows if o.get("over_budget")
        ]
        return {
            "weeks": weeks,
            "start": str(start or date.today()),
            "weekly_outflows": outflows,
            "total_projected_inr": str(to_money(total_projected)),
            "over_budget_weeks": over_budget_weeks,
            "discount_recommendations": recommendations,
        }
