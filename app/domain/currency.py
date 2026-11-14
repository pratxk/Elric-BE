"""Currency normalisation and single-currency totals.

Elric never converts between currencies (no FX rates in the demo), so
money must never be summed across currencies and then labelled as one
of them. ``single_currency_total`` groups amounts by their normalised
currency: when every amount shares one currency it returns that sum
with the currency; when currencies are mixed it returns only the INR
subset with "INR", so an ``_inr`` field is always genuinely INR.
"""

from __future__ import annotations

from decimal import Decimal

_SYMBOL_TO_CODE = {"$": "USD", "₹": "INR", "£": "GBP", "€": "EUR"}


def normalize_currency(value) -> str:
    """Extractor stores the currency verbatim ("$" for USD, "INR", ...);
    normalise it to an ISO code. Empty/unknown -> workspace INR."""
    if value is None:
        return "INR"
    v = str(value).strip()
    if not v:
        return "INR"
    if v in _SYMBOL_TO_CODE:
        return _SYMBOL_TO_CODE[v]
    return v.upper()


def single_currency_total(amounts) -> tuple[Decimal, str]:
    """``amounts``: iterable of (currency, amount). Returns (total,
    currency). Mixed currencies -> INR subset only, currency "INR"."""
    totals: dict[str, Decimal] = {}
    for currency, amount in amounts:
        code = normalize_currency(currency)
        totals[code] = totals.get(code, Decimal("0.00")) + Decimal(str(amount))
    if len(totals) == 1:
        code, total = next(iter(totals.items()))
        return total, code
    return totals.get("INR", Decimal("0.00")), "INR"
