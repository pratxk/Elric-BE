"""Config-driven tolerance engine (§5, §16a.3).

Rules live in config, never in prompts:
- quantity must match exactly (qty tolerance 0 by default)
- unit price within ±price_pct or ±price_abs, whichever is larger
- tax within rounding (tax tolerance, default ₹1.00)
- cumulative variance budgets: small variances auto-approve but count
  against a monthly per-vendor budget (v1.1: budget tracking in the service).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.money import to_money


@dataclass(frozen=True)
class ToleranceConfig:
    price_pct: Decimal = Decimal("0.02")  # ±2%
    price_abs: Decimal = Decimal("500")  # or ₹500, whichever larger
    qty_tolerance: Decimal = Decimal("0")  # exact by default
    tax_tolerance: Decimal = Decimal("1.00")  # rounding
    total_tolerance: Decimal = Decimal("1.00")


DEFAULT_TOLERANCE = ToleranceConfig()

# Per-vendor / per-category overrides, keyed by vendor id or category.
# Empty in the scaffold; populated from policy config in production.
VENDOR_TOLERANCES: dict[str, ToleranceConfig] = {}


def get_tolerance(vendor_id: str | None = None) -> ToleranceConfig:
    if vendor_id and vendor_id in VENDOR_TOLERANCES:
        return VENDOR_TOLERANCES[vendor_id]
    return DEFAULT_TOLERANCE


def price_within_tolerance(
    invoice_price: Decimal, po_price: Decimal, cfg: ToleranceConfig
) -> bool:
    allowed = max(to_money(po_price * cfg.price_pct), cfg.price_abs)
    return abs(to_money(invoice_price) - to_money(po_price)) <= allowed


def qty_within_tolerance(
    invoice_qty: Decimal, po_qty: Decimal, cfg: ToleranceConfig
) -> bool:
    return abs(to_money(invoice_qty) - to_money(po_qty)) <= cfg.qty_tolerance


def tax_within_tolerance(
    invoice_tax: Decimal, expected_tax: Decimal, cfg: ToleranceConfig
) -> bool:
    return abs(to_money(invoice_tax) - to_money(expected_tax)) <= cfg.tax_tolerance
