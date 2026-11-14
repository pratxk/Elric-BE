"""Synthetic corpus generator (§9).

Seeded generation of POs / receipts / invoices with injected mismatches,
so the matcher can be scored deterministically. Scenarios:
perfect, price_variance, qty_short, duplicate, tax_mismatch,
total_mismatch, missing_po.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from app.domain.models import (
    GoodsReceipt,
    GRLine,
    Invoice,
    InvoiceLine,
    POLine,
    PurchaseOrder,
    Vendor,
)

VENDOR = Vendor(id="vendor-acme", name="Acme Corporation Pvt Ltd", state_code="29")
BUYER_STATE = "29"  # Karnataka (intra-state for Acme)


def _base_po(seed: int) -> tuple[PurchaseOrder, GoodsReceipt]:
    rng = random.Random(seed)
    lines = [
        POLine(
            description="Laptop Dell Latitude 5540",
            hsn_code="8471",
            qty=Decimal("2"),
            unit_price=Decimal("65000"),
            tax_rate_pct=Decimal("18"),
        ),
        POLine(
            description="Wireless Mouse Logitech MX",
            hsn_code="8471",
            qty=Decimal("5"),
            unit_price=Decimal("2500"),
            tax_rate_pct=Decimal("18"),
        ),
    ]
    po = PurchaseOrder(
        id=f"po-{seed}",
        po_number=f"PO-2026-{1000 + seed}",
        vendor_id=VENDOR.id,
        po_date=date(2026, 11, 1),
        lines=lines,
        total=Decimal("168150"),
    )
    gr = GoodsReceipt(
        id=f"gr-{seed}",
        gr_number=f"GR-2026-{1000 + seed}",
        po_id=po.id,
        receipt_date=date(2026, 11, 10),
        lines=[
            GRLine(description="Laptop Dell Latitude 5540", qty_received=Decimal("2")),
            GRLine(description="Wireless Mouse Logitech MX", qty_received=Decimal("5")),
        ],
    )
    return po, gr


def _invoice_from_po(
    po: PurchaseOrder,
    seed: int,
    *,
    price_bump: Decimal | None = None,
    qty_short_line: int | None = None,
    tax_rate_override: Decimal | None = None,
    total_override: Decimal | None = None,
    invoice_number: str | None = None,
) -> Invoice:
    lines = []
    for idx, pl in enumerate(po.lines):
        qty = pl.qty
        if qty_short_line == idx:
            qty = pl.qty - Decimal("1")
        price = pl.unit_price + (price_bump or Decimal("0"))
        rate = tax_rate_override if tax_rate_override is not None else pl.tax_rate_pct
        lines.append(
            InvoiceLine(
                description=pl.description,
                hsn_code=pl.hsn_code,
                qty=qty,
                unit_price=price,
                tax_rate_pct=rate,
            )
        )
    inv = Invoice(
        id=f"inv-{seed}",
        vendor_id=VENDOR.id,
        invoice_number=invoice_number or f"INV-2026-{2000 + seed}",
        invoice_date=date(2026, 11, 12),
        due_date=date(2026, 12, 12),
        lines=lines,
    )
    inv = inv.recompute_totals()
    # Intra-state: CGST + SGST split.
    from app.domain.money import split_gst

    split = split_gst(inv.tax_total, intra_state=True)
    inv = inv.model_copy(update=split)
    if total_override is not None:
        inv = inv.model_copy(update={"total": total_override})
    return inv


def generate_corpus(n_per_scenario: int = 2, seed: int = 42) -> list[dict]:
    """Returns a list of cases: {scenario, invoice, po, gr, existing, expected}."""
    cases: list[dict] = []
    idx = 0

    def next_case(scenario: str, **kwargs) -> None:
        nonlocal idx
        idx += 1
        po, gr = _base_po(seed + idx)
        existing: list[Invoice] = []
        expected_codes: list[str] = []
        inv_kwargs: dict = {}
        use_po: PurchaseOrder | None = po
        use_gr: GoodsReceipt | None = gr

        if scenario == "perfect":
            pass
        elif scenario == "price_variance":
            inv_kwargs["price_bump"] = Decimal("5000")  # >2% and >₹500 on laptops
            expected_codes = ["PRICE_VARIANCE"]
        elif scenario == "qty_short":
            inv_kwargs["qty_short_line"] = 0
            expected_codes = ["QTY_SHORT"]
        elif scenario == "duplicate":
            prior = _invoice_from_po(po, 9000 + idx)
            existing = [prior]
            inv_kwargs["invoice_number"] = prior.invoice_number
            expected_codes = ["DUPLICATE"]
        elif scenario == "tax_mismatch":
            inv_kwargs["tax_rate_override"] = Decimal("5")  # wrong slab for HSN 84xx
            expected_codes = ["TAX_MISMATCH"]
        elif scenario == "total_mismatch":
            inv_kwargs["total_override"] = Decimal("999999.99")
            expected_codes = ["TOTAL_MISMATCH"]
        elif scenario == "missing_po":
            use_po, use_gr = None, None
            expected_codes = ["MISSING_PO"]

        inv = _invoice_from_po(po, seed + idx, **inv_kwargs)
        cases.append(
            {
                "scenario": scenario,
                "invoice": inv,
                "po": use_po,
                "gr": use_gr,
                "existing": existing,
                "expected_codes": expected_codes,
                "buyer_state": BUYER_STATE,
                "seller_state": VENDOR.state_code,
            }
        )

    scenarios = [
        "perfect",
        "price_variance",
        "qty_short",
        "duplicate",
        "tax_mismatch",
        "total_mismatch",
        "missing_po",
    ]
    for _ in range(n_per_scenario):
        for scenario in scenarios:
            next_case(scenario)
    return cases
