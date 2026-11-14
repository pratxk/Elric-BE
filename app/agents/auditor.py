"""Auditor node — metrics + eval reporting hook."""

from __future__ import annotations

from app.agents.state import InvoiceState


def auditor_node(state: InvoiceState) -> dict:
    return {
        "audit": [f"auditor: run recorded for invoice {state.get('invoice_id')}"],
    }
