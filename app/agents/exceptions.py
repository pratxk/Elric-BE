"""Exception handler node — classifies mismatches, runs root-cause
analysis with cited evidence + recommended fix, and drafts the vendor
query. Drafts only: nothing is ever auto-sent to a vendor (§19a.5).

Root-cause reasoning goes through RootCauseService: LLM-backed when a
configured client is injected, deterministic templates otherwise (tests
and no-key runs). The service is module-level and injectable via
``set_root_cause_service``.
"""

from __future__ import annotations

from app.agents.state import InvoiceState
from app.domain.models import Invoice, Match
from app.services.root_cause import RootCauseService

_service = RootCauseService()


def set_root_cause_service(service: RootCauseService) -> None:
    """Test seam: swap the root-cause service (e.g. a fake LLM client)."""
    global _service
    _service = service


def exceptions_node(state: InvoiceState) -> dict:
    codes = [e.get("code") for e in state.get("exceptions", [])]
    extracted = state.get("extracted") or {}
    invoice = None
    match = None
    if extracted.get("invoice"):
        try:
            invoice = Invoice.model_validate(extracted["invoice"])
        except Exception:  # noqa: BLE001 — proceed without invoice context
            invoice = None
    if state.get("match"):
        try:
            match = Match.model_validate(state["match"])
        except Exception:  # noqa: BLE001 — proceed without match context
            match = None
    causes: list[dict] = []
    draft = None
    if invoice is not None:
        causes = _service.analyze(invoice, match, state.get("exceptions", []))
        vendor_name = state.get("vendor_name") or invoice.vendor_id
        draft = RootCauseService.draft_vendor_query(invoice, causes, vendor_name)
    else:
        draft_lines = [
            f"Subject: Query on invoice {state.get('invoice_number', '?')}",
            "",
            "Hello,",
            "",
            "We found the following mismatches while processing your invoice:",
        ]
        for exc in state.get("exceptions", []):
            draft_lines.append(f"- {exc.get('code')}: {exc.get('message', '')}")
        draft_lines += [
            "",
            "Could you please review and advise on the correction needed?",
            "Regards, AP Team",
            "(Draft only — not sent.)",
        ]
        draft = "\n".join(draft_lines)
    enriched = []
    cause_by_code = {c["code"]: c for c in causes}
    for exc in state.get("exceptions", []):
        item = dict(exc)
        cause = cause_by_code.get(exc.get("code"))
        if cause:
            item["root_cause"] = cause["root_cause"]
            item["recommended_fix"] = cause["recommended_fix"]
            item["evidence"] = cause["evidence"]
        enriched.append(item)
    return {
        "status": "flagged",
        "exceptions": enriched,
        "root_causes": causes,
        "audit": [
            f"exceptions: classified {codes}; root-cause analysis done "
            f"({len(causes)} causes); vendor-query draft prepared (not sent)"
        ],
        "vendor_query_draft": draft,
    }
