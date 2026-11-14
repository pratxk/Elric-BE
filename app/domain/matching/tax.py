"""India GST validation (§16a.5).

- Recompute tax from lines and compare with the invoice's stated tax.
- CGST/SGST for intra-state (buyer and seller in the same state),
  IGST for inter-state.
- Slab rates by HSN code (first 2 digits = chapter; common slabs mapped).
- Reverse-charge / TDS detection is a v1.1 stub (returns a flag, not a verdict).
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.models import Invoice
from app.domain.money import money_sum, split_gst, tax_amount, to_money

# GST slabs by HSN chapter (first 2 digits). Unknown chapters default to 18%.
HSN_SLABS: dict[str, Decimal] = {
    "84": Decimal("18"),  # machinery / computers
    "85": Decimal("18"),  # electronics
    "49": Decimal("12"),  # printed books / brochures
    "99": Decimal("18"),  # services (SAC 99xxxx)
    "10": Decimal("5"),  # cereals (illustrative)
    "87": Decimal("28"),  # vehicles
}

DEFAULT_SLAB = Decimal("18")


def slab_for_hsn(hsn_code: str | None) -> Decimal:
    if not hsn_code:
        return DEFAULT_SLAB
    return HSN_SLABS.get(hsn_code[:2], DEFAULT_SLAB)


def validate_tax(
    invoice: Invoice,
    buyer_state_code: str | None,
    seller_state_code: str | None,
) -> list[dict]:
    """Validate an invoice's tax. Returns a list of mismatch dicts (empty = OK).

    Each dict: {field, expected, actual, message}.

    When any line's tax rate is unknown (None — e.g. a US invoice where
    the model returned no per-line rate), GST cannot be recomputed from
    lines, so validation is skipped entirely: a non-GST invoice must
    never be flagged TAX_MISMATCH for missing Indian tax data. The
    header-level totals are still checked by the extraction self-check
    and the matcher's total rule.
    """
    mismatches: list[dict] = []
    if not invoice.tax_rates_known:
        return mismatches
    expected_tax = money_sum([ln.line_tax for ln in invoice.lines])  # type: ignore[arg-type]
    if abs(to_money(invoice.tax_total) - expected_tax) > Decimal("1.00"):
        mismatches.append(
            {
                "field": "tax_total",
                "expected": str(expected_tax),
                "actual": str(invoice.tax_total),
                "message": f"tax total {invoice.tax_total} != recomputed {expected_tax}",
            }
        )
    # Slab check per line.
    for idx, ln in enumerate(invoice.lines):
        expected_rate = slab_for_hsn(ln.hsn_code)
        if ln.hsn_code and ln.tax_rate_pct is not None and ln.tax_rate_pct != expected_rate:
            mismatches.append(
                {
                    "field": f"lines[{idx}].tax_rate_pct",
                    "expected": str(expected_rate),
                    "actual": str(ln.tax_rate_pct),
                    "message": (
                        f"line {idx} HSN {ln.hsn_code} slab is {expected_rate}%, "
                        f"invoice says {ln.tax_rate_pct}%"
                    ),
                }
            )
    # Split check: intra-state => CGST+SGST, inter-state => IGST.
    if buyer_state_code and seller_state_code:
        intra_state = buyer_state_code == seller_state_code
        split = split_gst(expected_tax, intra_state)
        for field in ("cgst", "sgst", "igst"):
            actual = to_money(getattr(invoice, field))
            if abs(actual - split[field]) > Decimal("1.00"):
                mismatches.append(
                    {
                        "field": field,
                        "expected": str(split[field]),
                        "actual": str(actual),
                        "message": (
                            f"{field.upper()} {actual} != expected {split[field]} "
                            f"({'intra' if intra_state else 'inter'}-state)"
                        ),
                    }
                )
    return mismatches


def reverse_charge_applicable(hsn_code: str | None) -> bool:
    """Stub (v1.1): reverse-charge detection by HSN/service category."""
    # Real RCM list lives in policy config; nothing is flagged in the scaffold.
    return False
