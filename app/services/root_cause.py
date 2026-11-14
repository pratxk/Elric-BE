"""Exception root-cause analysis + vendor query drafting (§16b.1/2).

The LLM earns its place here: given the full mismatch evidence (PO, receipt,
prior invoices), explain WHY the mismatch likely happened, with cited
evidence and a recommended fix — not just "price differs".

Two paths, same interface:
- LLM path: when a configured client is supplied, Gemini is asked for a
  JSON root-cause per exception code; the reply is parsed and validated,
  and any failure falls back to the template path (never blocks the queue).
- Template path (deterministic, no LLM): per-code analysis built purely
  from the cited numbers in the match result. This is what runs in tests
  and when no key is configured.

Vendor query drafts are template-built and cite exact lines/amounts.
Drafts only — nothing is ever sent (§19a.5).
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.domain.models import Invoice, Match

# Deterministic per-code explanations. {placeholders} are filled from the
# match rule outcomes / invoice numbers — every claim cites its evidence.
_CODE_TEMPLATES: dict[str, dict[str, str]] = {
    "PRICE_VARIANCE": {
        "cause": "Unit price on the invoice is higher than the PO price "
        "beyond the ±2% / ₹500 tolerance.",
        "fix": "Ask the vendor to confirm the price or issue a credit note "
        "for the difference; check whether a PO price revision was agreed.",
    },
    "QTY_SHORT": {
        "cause": "Invoiced quantity exceeds the PO quantity or the quantity "
        "received on the goods receipt — goods may be short-shipped or the "
        "invoice may cover a future delivery.",
        "fix": "Confirm the received quantity with stores; ask the vendor to "
        "correct the quantity or split the invoice across deliveries.",
    },
    "DUPLICATE": {
        "cause": "Same vendor + invoice number (or same vendor + amount "
        "within 90 days) already exists — likely a re-sent invoice or a "
        "double submission.",
        "fix": "Do not pay. Confirm with the vendor whether this is a "
        "re-issue of an already-paid invoice; mark duplicate if so.",
    },
    "MISSING_PO": {
        "cause": "No purchase order could be found for this invoice — it "
        "may be a non-PO purchase, or the PO number on the invoice is wrong.",
        "fix": "Ask the requester for the PO number, or route for non-PO "
        "approval if the spend is legitimate.",
    },
    "TAX_MISMATCH": {
        "cause": "Tax on the invoice does not recompute from the lines, or "
        "the GST slab / CGST-SGST vs IGST split is wrong for the HSN code "
        "and buyer/seller states.",
        "fix": "Ask the vendor to issue a corrected invoice with the right "
        "GST rate and split; do not claim input credit on a wrong invoice.",
    },
    "TOTAL_MISMATCH": {
        "cause": "The stated invoice total does not equal subtotal + tax "
        "recomputed from the lines — an arithmetic error on the invoice.",
        "fix": "Ask the vendor to re-issue with corrected totals.",
    },
}

ROOT_CAUSE_PROMPT = (
    "You are an accounts-payable analyst. Given the invoice, the purchase "
    "order, the goods receipt and the deterministic match result below, "
    "explain for EACH exception code the most likely root cause, cite the "
    "exact evidence (line numbers, amounts, PO values), and recommend one "
    "concrete fix. Return ONLY JSON: a list of objects with keys "
    "code, root_cause, evidence (list of strings), recommended_fix. "
    "Never invent PO/receipt values not in the input.\n\nEvidence:\n{evidence}"
)


class RootCauseService:
    """LLM-backed root-cause analysis with a deterministic fallback.

    ``client`` is any object with ``configured`` and ``generate_text`` —
    the real GeminiClient in production, a fake in tests. ``client=None``
    means template-only (no LLM call, ever).
    """

    def __init__(self, client=None):
        self.client = client

    def analyze(
        self, invoice: Invoice, match: Match | None, exceptions: list[dict]
    ) -> list[dict]:
        """One root-cause record per exception: {code, root_cause, evidence,
        recommended_fix}. LLM path when a configured client exists, else
        the deterministic template path."""
        if self.client is not None and getattr(self.client, "configured", False):
            try:
                return self._analyze_with_llm(invoice, match, exceptions)
            except Exception:  # noqa: BLE001 — LLM failure never blocks the queue
                pass
        return [self._template_cause(invoice, match, exc) for exc in exceptions]

    def _analyze_with_llm(
        self, invoice: Invoice, match: Match | None, exceptions: list[dict]
    ) -> list[dict]:
        evidence = _evidence_block(invoice, match, exceptions)
        raw = self.client.generate_text(
            ROOT_CAUSE_PROMPT.format(evidence=evidence)
        )
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("LLM root-cause reply is not a list")
        out: list[dict] = []
        for item in parsed:
            if not isinstance(item, dict) or not item.get("code"):
                raise ValueError("LLM root-cause item missing code")
            out.append(
                {
                    "code": str(item["code"]),
                    "root_cause": str(item.get("root_cause", "")),
                    "evidence": [str(e) for e in item.get("evidence", [])],
                    "recommended_fix": str(item.get("recommended_fix", "")),
                }
            )
        codes = {e.get("code") for e in exceptions}
        if {r["code"] for r in out} != codes:
            raise ValueError("LLM root-cause codes do not match exceptions")
        return out

    def _template_cause(
        self, invoice: Invoice, match: Match | None, exc: dict
    ) -> dict:
        code = str(exc.get("code", ""))
        template = _CODE_TEMPLATES.get(
            code,
            {
                "cause": "Unclassified mismatch — needs human review.",
                "fix": "Review the invoice against the PO and receipt manually.",
            },
        )
        evidence = [exc.get("message", "")] if exc.get("message") else []
        if match is not None:
            for rule in match.rule_outcomes:
                if not rule.passed and rule.detail:
                    evidence.append(f"{rule.rule}: {rule.detail}")
        return {
            "code": code,
            "root_cause": template["cause"],
            "evidence": evidence,
            "recommended_fix": template["fix"],
        }

    @staticmethod
    def draft_vendor_query(
        invoice: Invoice, causes: list[dict], vendor_name: str
    ) -> str:
        """Context-aware vendor query draft citing exact lines/amounts."""
        lines = [
            f"Subject: Query on invoice {invoice.invoice_number} "
            f"(₹{invoice.total})",
            "",
            f"Hello {vendor_name},",
            "",
            "We found the following mismatches while processing your invoice:",
        ]
        for cause in causes:
            lines.append(f"- {cause['code']}: {cause['root_cause']}")
            for ev in cause.get("evidence", [])[:2]:
                lines.append(f"  Evidence: {ev}")
            lines.append(f"  Suggested fix: {cause['recommended_fix']}")
        lines += [
            "",
            "Could you please review and advise on the correction needed?",
            "",
            "Regards, AP Team",
            "(Draft only — not sent.)",
        ]
        return "\n".join(lines)


def _evidence_block(
    invoice: Invoice, match: Match | None, exceptions: list[dict]
) -> str:
    parts = [
        f"Invoice {invoice.invoice_number} vendor={invoice.vendor_id} "
        f"total=₹{invoice.total} subtotal=₹{invoice.subtotal} "
        f"tax=₹{invoice.tax_total} CGST=₹{invoice.cgst} SGST=₹{invoice.sgst} "
        f"IGST=₹{invoice.igst}",
    ]
    for idx, ln in enumerate(invoice.lines):
        rate = f"{ln.tax_rate_pct}%" if ln.tax_rate_pct is not None else "n/a"
        parts.append(
            f"  line {idx}: {ln.description} qty={ln.qty} "
            f"unit_price=₹{ln.unit_price} tax_rate={rate}"
        )
    if match is not None:
        parts.append(f"Match outcome={match.outcome.value} po_id={match.po_id}")
        for rule in match.rule_outcomes:
            parts.append(
                f"  rule {rule.rule}: {'pass' if rule.passed else 'FAIL'} "
                f"{rule.detail}"
            )
    for exc in exceptions:
        parts.append(f"Exception {exc.get('code')}: {exc.get('message', '')}")
    return "\n".join(parts)
