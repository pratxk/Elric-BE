"""AP aging — buckets computed from the store (§16a.14).

Buckets by days overdue vs the due date (as-of date defaults to today):
current (not yet due), overdue 1–30, 31–60, 61–90, 90+. Each bucket
carries a count and a total. Overdue alerts surface in metrics.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

BUCKETS = ("current", "overdue_1_30", "overdue_31_60", "overdue_61_90", "overdue_90_plus")

# "Due soon": an open invoice whose due date falls within this many days.
DUE_SOON_DAYS = 7


def bucket_for(due: date | None, as_of: date) -> str:
    if due is None:
        return "current"
    overdue_days = (as_of - due).days
    if overdue_days <= 0:
        return "current"
    if overdue_days <= 30:
        return "overdue_1_30"
    if overdue_days <= 60:
        return "overdue_31_60"
    if overdue_days <= 90:
        return "overdue_61_90"
    return "overdue_90_plus"


def compute_aging(invoices: list, as_of: date | None = None) -> dict:
    """``invoices``: objects/dicts with due_date (date|str|None), total,
    status. Only open invoices count (received/extracted/validated/
    matched/flagged/approved/posted/scheduled/on_hold); paid, rejected
    and duplicate invoices are out of aging."""
    as_of = as_of or date.today()
    open_statuses = {
        "received", "extracted", "validated", "matched", "flagged",
        "approved", "posted", "scheduled", "on_hold",
    }
    buckets: dict[str, dict] = {
        b: {"count": 0, "total_inr": Decimal("0.00")} for b in BUCKETS
    }
    oldest_overdue_days = 0
    for inv in invoices:
        status = inv.get("status") if isinstance(inv, dict) else getattr(inv, "status", None)
        if status is not None and status not in open_statuses:
            continue
        due = inv.get("due_date") if isinstance(inv, dict) else getattr(inv, "due_date", None)
        if isinstance(due, str):
            due = date.fromisoformat(due) if due else None
        total = inv.get("total", 0) if isinstance(inv, dict) else getattr(inv, "total", 0)
        bucket = bucket_for(due, as_of)
        buckets[bucket]["count"] += 1
        buckets[bucket]["total_inr"] += Decimal(str(total))
        if due is not None:
            oldest_overdue_days = max(oldest_overdue_days, (as_of - due).days)
    overdue_count = sum(buckets[b]["count"] for b in BUCKETS[1:])
    overdue_total = sum(
        (buckets[b]["total_inr"] for b in BUCKETS[1:]), Decimal("0.00")
    )
    # Due soon: open invoices due within DUE_SOON_DAYS (not yet overdue).
    due_soon_count = 0
    due_soon_total = Decimal("0.00")
    for inv in invoices:
        status = inv.get("status") if isinstance(inv, dict) else getattr(inv, "status", None)
        if status is not None and status not in open_statuses:
            continue
        due = inv.get("due_date") if isinstance(inv, dict) else getattr(inv, "due_date", None)
        if isinstance(due, str):
            due = date.fromisoformat(due) if due else None
        if due is None:
            continue
        if 0 < (due - as_of).days <= DUE_SOON_DAYS:
            due_soon_count += 1
            total = inv.get("total", 0) if isinstance(inv, dict) else getattr(inv, "total", 0)
            due_soon_total += Decimal(str(total))
    return {
        "as_of": str(as_of),
        "buckets": {
            b: {"count": v["count"], "total_inr": str(v["total_inr"])}
            for b, v in buckets.items()
        },
        "overdue_count": overdue_count,
        "overdue_total_inr": str(overdue_total),
        "oldest_overdue_days": oldest_overdue_days,
        "due_soon_count": due_soon_count,
        "due_soon_total_inr": str(due_soon_total),
    }
