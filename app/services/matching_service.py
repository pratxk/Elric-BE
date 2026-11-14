"""Deterministic matching service.

Wires the domain matching strategies into the 3-way match:
invoice vs PO vs goods receipt, with duplicate + tax + totals checks.
Pure orchestration over pure domain code — no LLM, no framework logic.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.matching import aligner, duplicate, tax as tax_mod
from app.domain.matching.tolerance import (
    ToleranceConfig,
    get_tolerance,
    price_within_tolerance,
    qty_within_tolerance,
    tax_within_tolerance,
)
from app.domain.models import (
    ExceptionCode,
    GoodsReceipt,
    Invoice,
    Match,
    MatchOutcome,
    PurchaseOrder,
    RuleOutcome,
)
from app.domain.money import money_sum, to_money


class MatchingService:
    def __init__(self, tolerance: ToleranceConfig | None = None):
        self.tolerance = tolerance or get_tolerance()

    def match(
        self,
        invoice: Invoice,
        po: PurchaseOrder | None,
        gr: GoodsReceipt | None,
        existing_invoices: list[Invoice] | None = None,
        paid_invoices: list[Invoice] | None = None,
        buyer_state_code: str | None = None,
        seller_state_code: str | None = None,
    ) -> Match:
        existing = existing_invoices or []
        rules: list[RuleOutcome] = []
        codes: set[ExceptionCode] = set()

        # 1. Duplicate check — before any LLM call in the real pipeline,
        #    and first here: a duplicate never auto-approves.
        dup_kind, dup_of = duplicate.check_duplicate(invoice, existing, paid_invoices)
        if dup_kind:
            rules.append(
                RuleOutcome(
                    rule="duplicate_check",
                    passed=False,
                    detail=f"{dup_kind} duplicate of invoice {dup_of.id}",
                )
            )
            codes.add(ExceptionCode.DUPLICATE)
            return Match(
                invoice_id=invoice.id,
                po_id=po.id if po else None,
                gr_id=gr.id if gr else None,
                outcome=MatchOutcome.DUPLICATE,
                exception_codes=sorted(codes),
                rule_outcomes=rules,
            )
        rules.append(RuleOutcome(rule="duplicate_check", passed=True))

        # 2. PO must exist.
        if po is None:
            rules.append(
                RuleOutcome(rule="po_exists", passed=False, detail="no PO found")
            )
            codes.add(ExceptionCode.MISSING_PO)
            return Match(
                invoice_id=invoice.id,
                outcome=MatchOutcome.MISSING_PO,
                exception_codes=sorted(codes),
                rule_outcomes=rules,
            )
        rules.append(RuleOutcome(rule="po_exists", passed=True))

        # 3. Line-item alignment (Hungarian) + per-pair qty/price checks.
        pairs = aligner.align_lines(
            [
                {
                    "description": ln.description,
                    "qty": ln.qty,
                    "unit_price": ln.unit_price,
                }
                for ln in invoice.lines
            ],
            [
                {
                    "description": ln.description,
                    "qty": ln.qty,
                    "unit_price": ln.unit_price,
                }
                for ln in po.lines
            ],
        )
        aligned_inv = {p["invoice_idx"] for p in pairs}
        if len(pairs) != len(invoice.lines) or len(pairs) != len(po.lines):
            rules.append(
                RuleOutcome(
                    rule="line_alignment",
                    passed=False,
                    detail=f"aligned {len(pairs)} of {len(invoice.lines)} invoice "
                    f"lines / {len(po.lines)} PO lines",
                )
            )
        else:
            rules.append(RuleOutcome(rule="line_alignment", passed=True))

        for pair in pairs:
            inv_ln = invoice.lines[pair["invoice_idx"]]
            po_ln = po.lines[pair["po_idx"]]
            if not qty_within_tolerance(inv_ln.qty, po_ln.qty, self.tolerance):
                codes.add(ExceptionCode.QTY_SHORT)
                rules.append(
                    RuleOutcome(
                        rule="qty_match",
                        passed=False,
                        detail=f"line {pair['invoice_idx']}: qty {inv_ln.qty} "
                        f"vs PO {po_ln.qty}",
                    )
                )
            if not price_within_tolerance(
                inv_ln.unit_price, po_ln.unit_price, self.tolerance
            ):
                codes.add(ExceptionCode.PRICE_VARIANCE)
                rules.append(
                    RuleOutcome(
                        rule="price_match",
                        passed=False,
                        detail=f"line {pair['invoice_idx']}: price {inv_ln.unit_price} "
                        f"vs PO {po_ln.unit_price}",
                    )
                )

        # 4. Goods receipt quantity check (3-way: received vs invoiced).
        if gr is not None:
            received_total = money_sum([ln.qty_received for ln in gr.lines])
            invoiced_total = money_sum([ln.qty for ln in invoice.lines])
            if invoiced_total > received_total:
                codes.add(ExceptionCode.QTY_SHORT)
                rules.append(
                    RuleOutcome(
                        rule="receipt_qty",
                        passed=False,
                        detail=f"invoiced qty {invoiced_total} > received {received_total}",
                    )
                )
            else:
                rules.append(RuleOutcome(rule="receipt_qty", passed=True))

        # 5. Totals recomputed from lines.
        recomputed = invoice.recompute_totals()
        if to_money(recomputed.total) != to_money(invoice.total):
            codes.add(ExceptionCode.TOTAL_MISMATCH)
            rules.append(
                RuleOutcome(
                    rule="total_recompute",
                    passed=False,
                    detail=f"stated total {invoice.total} != recomputed {recomputed.total}",
                )
            )
        else:
            rules.append(RuleOutcome(rule="total_recompute", passed=True))

        # 6. Tax validation (GST).
        tax_issues = tax_mod.validate_tax(
            invoice, buyer_state_code, seller_state_code
        )
        if tax_issues:
            codes.add(ExceptionCode.TAX_MISMATCH)
            for issue in tax_issues:
                rules.append(
                    RuleOutcome(rule="tax_validation", passed=False, detail=issue["message"])
                )
        else:
            rules.append(RuleOutcome(rule="tax_validation", passed=True))

        outcome = MatchOutcome.CLEAN if not codes else MatchOutcome.VARIANCE
        return Match(
            invoice_id=invoice.id,
            po_id=po.id,
            gr_id=gr.id if gr else None,
            outcome=outcome,
            exception_codes=sorted(codes),
            rule_outcomes=rules,
            aligned_pairs=pairs,
        )
