"""Money helpers. Pure Python — no framework imports.

Money is Decimal, never float. All amounts are rounded to 2dp (paise)
with ROUND_HALF_UP at the boundary of each computation.
"""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_money(value: Decimal | int | str) -> Decimal:
    """Coerce to a 2dp Decimal. Raises on float input — floats are banned."""
    if isinstance(value, float):
        raise TypeError("money must never be a float; pass Decimal/int/str")
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money_sum(values: list[Decimal]) -> Decimal:
    return sum((to_money(v) for v in values), Decimal("0.00"))


def tax_amount(base: Decimal, rate_pct: Decimal) -> Decimal:
    """Tax on a taxable base at a percentage rate, rounded to paise."""
    return to_money(to_money(base) * to_money(rate_pct) / Decimal("100"))


def split_gst(tax: Decimal, intra_state: bool) -> dict[str, Decimal]:
    """Split a GST amount into CGST/SGST (intra-state) or IGST (inter-state).

    Intra-state: tax is split equally between CGST and SGST (each half,
    rounded so the two halves sum exactly to the tax). Inter-state: all IGST.
    """
    tax = to_money(tax)
    if intra_state:
        cgst = (tax / 2).quantize(CENT, rounding=ROUND_HALF_UP)
        return {"cgst": cgst, "sgst": to_money(tax - cgst), "igst": Decimal("0.00")}
    return {"cgst": Decimal("0.00"), "sgst": Decimal("0.00"), "igst": tax}


def early_payment_discount_value(
    amount: Decimal,
    discount_pct: Decimal,
    days_early: int,
    cost_of_capital_annual_pct: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    """NPV analysis for terms like 2/10 Net 30.

    Returns (discount_gained, opportunity_cost_of_paying_early, pay_early?).
    Pay early only when the discount exceeds the cost of holding the cash.
    """
    amount = to_money(amount)
    discount = to_money(amount * to_money(discount_pct) / Decimal("100"))
    # Cost of paying `days_early` sooner at the annual cost of capital.
    opportunity_cost = to_money(
        amount
        * to_money(cost_of_capital_annual_pct)
        / Decimal("100")
        * Decimal(days_early)
        / Decimal("365")
    )
    return discount, opportunity_cost, discount > opportunity_cost
