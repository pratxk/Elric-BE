"""Bookkeeper node — posts approved invoices to the ledger (SQLite) and
schedules payment. Payments are scheduled, never executed, without human
approval (§19a.5).

Posting is idempotent per invoice (the service skips an already-posted
invoice), so a resumed run or a retried node never double-posts. If the
invoice row isn't in the store (graph unit tests), the entries are still
computed and reported, just not persisted.
"""

from __future__ import annotations

from app.agents.state import InvoiceState
from app.domain.models import Invoice
from app.infra.store import store
from app.services.bookkeeping_service import BookkeepingService

_service = BookkeepingService()


def bookkeeper_node(state: InvoiceState) -> dict:
    extracted = state.get("extracted") or {}
    if not extracted.get("invoice"):
        return {
            "status": "approved",
            "audit": ["bookkeeper: no extracted invoice — nothing to post"],
        }
    invoice = Invoice.model_validate(extracted["invoice"])
    invoice_id = state.get("invoice_id") or invoice.id
    # Persist only when the invoice row exists in the store (real uploads).
    # Synthetic eval / graph-test ids have no row — compute the posting
    # without persisting, so eval runs never pollute the ledger.
    persist_store = store if store.get_invoice(invoice_id) is not None else None
    try:
        result = _service.post_invoice(
            invoice,
            payment_terms=state.get("payment_terms") or invoice.payment_terms,
            store=persist_store,
        )
    except Exception as exc:  # noqa: BLE001 — never crash the graph on posting
        return {
            "status": "approved",
            "audit": [f"bookkeeper: posting failed — {exc}; invoice stays approved"],
        }
    if result.get("already_posted"):
        return {
            "status": "posted",
            "ledger_posted": True,
            "payment_scheduled_date": result.get("scheduled_date"),
            "audit": [
                f"bookkeeper: invoice {invoice_id} already posted — skipped "
                "(idempotent)"
            ],
        }
    discount = result.get("discount") or {}
    return {
        "status": "posted",
        "ledger_posted": True,
        "payment_scheduled_date": result.get("scheduled_date"),
        "audit": [
            f"bookkeeper: invoice {invoice_id} posted to ledger "
            f"(Dr 5000 / Cr AP-2000 ₹{invoice.total}); payment scheduled "
            f"for {result.get('scheduled_date')} (not executed)"
            + (
                f"; discount: {discount.get('recommendation')} "
                f"(save ₹{discount.get('net_saving_inr')})"
                if discount
                else ""
            )
        ],
    }
