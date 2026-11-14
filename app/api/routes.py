"""FastAPI routers (§8). Thin: validate → service → respond.

Upload runs the LangGraph pipeline in a BackgroundTask. The graph run is
streamed (``stream_mode="updates"``) so every node event lands in the
append-only audit log as it happens — the SSE trace endpoint then streams
that stored trail, which is the live trace. A run that pauses at the
approval interrupt is resumed by the approve/reject endpoints with a real
``Command(resume=...)``; the bookkeeper then posts to the ledger.

When extraction is unavailable (no Gemini key) the graph parks the
invoice on_hold — never faked.
"""

from __future__ import annotations

import json
import statistics
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from app.agents.ingestor import document_hash
from app.agents.matcher import _row_to_invoice
from app.domain.lifecycle import transition
from app.domain.models import (
    GoodsReceipt,
    InvoiceStatus,
)
from app.eval.metrics import run_graph_eval, run_synthetic_eval
from app.graph import get_graph
from app.infra import docstore
from app.infra.store import store
from app.services.aging_service import compute_aging
from app.services.approval_service import ApprovalService
from app.services.bookkeeping_service import BookkeepingService
from app.services.forecast_service import ForecastService
from app.services.root_cause import RootCauseService

from .schemas import (
    AgingOut,
    DecisionIn,
    DiscountIn,
    EvalRunOut,
    InvoiceOut,
    LedgerOut,
    MetricsOut,
    PaymentOut,
    PurchaseOrderIn,
    VendorQueryOut,
)

router = APIRouter()
approval_service = ApprovalService()
bookkeeping_service = BookkeepingService()

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
}


def _row_to_out(row) -> InvoiceOut:
    match = json.loads(row.match_json) if row.match_json else None
    return InvoiceOut(
        id=row.id,
        vendor_id=row.vendor_id,
        vendor_name=row.vendor_name,
        invoice_number=row.invoice_number,
        invoice_date=row.invoice_date,
        due_date=row.due_date,
        currency=row.currency,
        status=row.status,
        subtotal=row.subtotal,
        tax_total=row.tax_total,
        total=row.total,
        document_hash=row.document_hash,
        lines=row.lines(),
        match=match,
        exceptions=row.exceptions(),
        payment_terms=row.payment_terms,
    )


def _sync_state_to_store(invoice_id: str, state: dict) -> None:
    """Persist a graph run's final state back to the invoice row."""
    row = store.get_invoice(invoice_id)
    if row is None:
        return
    fields: dict = {}
    if state.get("status"):
        fields["status"] = state["status"]
    if state.get("vendor_name"):
        fields["vendor_name"] = state["vendor_name"]
    if state.get("invoice_number"):
        fields["invoice_number"] = state["invoice_number"]
    extracted = state.get("extracted") or {}
    inv = extracted.get("invoice") or {}
    if inv:
        fields.update(
            {
                "vendor_id": inv.get("vendor_id", row.vendor_id),
                "invoice_number": inv.get("invoice_number", row.invoice_number),
                "invoice_date": str(inv.get("invoice_date", row.invoice_date)),
                "due_date": str(inv["due_date"]) if inv.get("due_date") else None,
                "currency": inv.get("currency", row.currency),
                "subtotal": inv.get("subtotal", row.subtotal),
                "tax_total": inv.get("tax_total", row.tax_total),
                "total": inv.get("total", row.total),
                "lines": inv.get("lines", []),
                "payment_terms": inv.get("payment_terms"),
            }
        )
        if extracted.get("field_confidences"):
            fields["extraction"] = {
                "field_confidences": extracted["field_confidences"]
            }
    if state.get("match") is not None:
        fields["match"] = state["match"]
    if state.get("exceptions") is not None:
        fields["exceptions"] = state["exceptions"]
    store.update_invoice(invoice_id, **fields)
    if state.get("auto_approved"):
        store.record_audit(
            "auto_approved", actor="auto", invoice_id=invoice_id
        )


def _graph_paused(invoice_id: str) -> bool:
    """True when a run for this invoice is waiting at the approval interrupt."""
    try:
        snapshot = get_graph().get_state(
            {"configurable": {"thread_id": invoice_id}}
        )
        return any(
            getattr(task, "interrupts", None) for task in (snapshot.tasks or [])
        )
    except Exception:  # noqa: BLE001 — no checkpoint / no run yet
        return False


def _stream_graph(invoice_id: str, payload) -> tuple[dict, bool]:
    """Stream a graph invocation (initial state or Command), recording
    every node event to the audit log. Returns (final_state, paused)."""
    graph = get_graph()
    config = {"configurable": {"thread_id": invoice_id}}
    try:
        for chunk in graph.stream(payload, config=config, stream_mode="updates"):
            for node_name in chunk:
                if node_name == "__interrupt__":
                    store.record_audit(
                        "graph_paused_awaiting_human",
                        actor="system",
                        invoice_id=invoice_id,
                    )
                else:
                    store.record_audit(
                        f"node:{node_name}", actor="system", invoice_id=invoice_id
                    )
    except Exception as exc:  # noqa: BLE001 — park the invoice, don't crash
        store.record_audit(
            "graph_run_failed", actor="system", invoice_id=invoice_id,
            error=str(exc),
        )
        raise
    snapshot = graph.get_state(config)
    state = dict(snapshot.values or {})
    paused = any(
        getattr(task, "interrupts", None) for task in (snapshot.tasks or [])
    )
    return state, paused


def _run_graph(invoice_id: str, doc_hash: str) -> None:
    """Background graph run for an uploaded invoice."""
    try:
        state, paused = _stream_graph(
            invoice_id,
            {
                "invoice_id": invoice_id,
                "document_hash": doc_hash,
                "audit": [],
            },
        )
        _sync_state_to_store(invoice_id, state)
        row = store.get_invoice(invoice_id)
        if row is not None and row.status == "received":
            # The run ended without moving the invoice (e.g. extraction
            # parked it, or the state sync found no status): flag it so a
            # human sees it instead of it sitting as "received" forever.
            store.update_invoice(invoice_id, status="flagged")
    except Exception:  # noqa: BLE001 — park the invoice, don't crash
        row = store.get_invoice(invoice_id)
        if row is not None and row.status == "received":
            store.update_invoice(invoice_id, status="flagged")
    finally:
        row = store.get_invoice(invoice_id)
        if row is not None:
            store.record_audit(
                "graph_run_finished",
                actor="system",
                invoice_id=invoice_id,
                status=row.status,
            )


def _resume_graph(invoice_id: str, decision: str) -> dict | None:
    """Resume a paused run with the human decision. Returns the final
    state, or None when no run was paused."""
    if not _graph_paused(invoice_id):
        return None
    state, _paused = _stream_graph(invoice_id, Command(resume=decision))
    _sync_state_to_store(invoice_id, state)
    return state


def _invoice_from_row(row) -> object | None:
    return _row_to_invoice(row)


# -- invoices -------------------------------------------------------------


@router.post("/invoices/upload", response_model=InvoiceOut, status_code=202)
async def upload_invoice(
    background_tasks: BackgroundTasks, file: UploadFile = File(...)
):
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file type {file.content_type}; PDF/PNG/JPEG only",
        )
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds 20 MB")
    doc_hash = document_hash(content)

    # Hash dedupe before any LLM call (§19a.1).
    for row in store.list_invoices():
        if row.document_hash == doc_hash:
            raise HTTPException(
                status_code=409,
                detail=f"duplicate document already uploaded as invoice {row.id}",
            )

    invoice_id = str(uuid.uuid4())
    docstore.register(invoice_id, content)
    row = store.create_invoice(
        {
            "id": invoice_id,
            "invoice_number": file.filename or "unknown",
            "invoice_date": "",
            "status": InvoiceStatus.RECEIVED.value,
            "document_hash": doc_hash,
        }
    )
    store.record_audit(
        "upload", actor="system", invoice_id=invoice_id, filename=file.filename
    )
    background_tasks.add_task(_run_graph, invoice_id, doc_hash)
    return _row_to_out(row)


@router.get("/invoices", response_model=list[InvoiceOut])
def list_invoices(status: str | None = None):
    return [_row_to_out(r) for r in store.list_invoices(status=status)]


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: str):
    row = store.get_invoice(invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    return _row_to_out(row)


@router.post("/invoices/{invoice_id}/approve", response_model=InvoiceOut)
def approve_invoice(invoice_id: str, body: DecisionIn):
    row = store.get_invoice(invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    # Real HITL resume: if a graph run is paused at the approval
    # interrupt, the decision resumes it and the bookkeeper posts.
    if _graph_paused(invoice_id):
        _resume_graph(invoice_id, "approved")
        store.record_audit(
            "approve", actor=body.approver, invoice_id=invoice_id,
            reason=body.reason,
        )
        return _row_to_out(store.get_invoice(invoice_id))
    # No paused run (e.g. already matched, or a direct approval): apply
    # the lifecycle transition and post via the bookkeeper service.
    try:
        new_status = transition(InvoiceStatus(row.status), "approve")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    store.update_invoice(invoice_id, status=new_status.value)
    store.record_audit(
        "approve", actor=body.approver, invoice_id=invoice_id, reason=body.reason
    )
    invoice = _invoice_from_row(store.get_invoice(invoice_id))
    if invoice is not None:
        bookkeeping_service.post_invoice(invoice, store=store)
        store.update_invoice(invoice_id, status=InvoiceStatus.POSTED.value)
        store.record_audit(
            "booked", actor="system", invoice_id=invoice_id,
            status=InvoiceStatus.POSTED.value,
        )
    return _row_to_out(store.get_invoice(invoice_id))


@router.post("/invoices/{invoice_id}/reject", response_model=InvoiceOut)
def reject_invoice(invoice_id: str, body: DecisionIn):
    row = store.get_invoice(invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    if _graph_paused(invoice_id):
        _resume_graph(invoice_id, "rejected")
        store.record_audit(
            "reject", actor=body.approver, invoice_id=invoice_id,
            reason=body.reason,
        )
        return _row_to_out(store.get_invoice(invoice_id))
    try:
        new_status = transition(InvoiceStatus(row.status), "reject")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    store.update_invoice(invoice_id, status=new_status.value)
    store.record_audit(
        "reject", actor=body.approver, invoice_id=invoice_id, reason=body.reason
    )
    return _row_to_out(store.get_invoice(invoice_id))


@router.post("/invoices/{invoice_id}/query-vendor", response_model=VendorQueryOut)
def query_vendor(invoice_id: str):
    """Draft only — returns a vendor query draft, never sends it."""
    row = store.get_invoice(invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    invoice = _invoice_from_row(row)
    causes = [
        {
            "code": exc.get("code", ""),
            "root_cause": exc.get("root_cause") or exc.get("message", ""),
            "evidence": exc.get("evidence", [exc.get("message", "")])
            if exc.get("message")
            else [],
            "recommended_fix": exc.get("recommended_fix", ""),
        }
        for exc in row.exceptions()
    ]
    if invoice is not None and causes:
        draft = RootCauseService.draft_vendor_query(
            invoice, causes, row.vendor_name or invoice.vendor_id
        )
    else:
        lines = [
            f"Subject: Query on invoice {row.invoice_number or row.id}",
            "",
            "Hello,",
            "",
            "We found the following mismatches while processing your invoice:",
        ]
        for exc in row.exceptions():
            lines.append(f"- {exc.get('code')}: {exc.get('message', '')}")
        lines += [
            "",
            "Could you please review and advise on the correction needed?",
            "Regards, AP Team",
            "(Draft only — not sent.)",
        ]
        draft = "\n".join(lines)
    store.record_audit(
        "vendor_query_drafted", actor="system", invoice_id=invoice_id
    )
    return VendorQueryOut(invoice_id=invoice_id, draft=draft, sent=False)


@router.get("/invoices/{invoice_id}/trace")
def invoice_trace(invoice_id: str):
    """SSE: streams the stored audit trail for the invoice (including the
    per-node events recorded live during the graph run), then a done
    event. Same pattern as Almighty's /api/chat/stream."""
    row = store.get_invoice(invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="invoice not found")

    def events():
        for entry in store.audit_for_invoice(invoice_id):
            payload = json.dumps(
                {
                    "action": entry.action,
                    "actor": entry.actor,
                    "detail": json.loads(entry.detail_json or "{}"),
                    "at": entry.created_at.isoformat(),
                },
                default=str,
            )
            yield f"data: {payload}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/invoices/{invoice_id}/ledger", response_model=list[LedgerOut])
def invoice_ledger(invoice_id: str):
    if store.get_invoice(invoice_id) is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    return [
        LedgerOut(
            id=r.id,
            invoice_id=r.invoice_id,
            account=r.account,
            debit=r.debit,
            credit=r.credit,
            posted_at=r.posted_at.isoformat(),
        )
        for r in store.ledger_for_invoice(invoice_id)
    ]


@router.get("/invoices/{invoice_id}/payments", response_model=list[PaymentOut])
def invoice_payments(invoice_id: str):
    if store.get_invoice(invoice_id) is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    return [
        PaymentOut(
            id=r.id,
            invoice_id=r.invoice_id,
            amount=r.amount,
            scheduled_date=r.scheduled_date,
            status=r.status,
        )
        for r in store.payments_for_invoice(invoice_id)
    ]


# -- purchase orders / goods receipts --------------------------------------


@router.post("/purchase-orders", status_code=201)
def create_purchase_order(po: PurchaseOrderIn):
    row = store.create_po(
        {
            "id": str(uuid.uuid4()),
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "vendor_name": po.vendor_name,
            "po_date": po.po_date,
            "total": po.total,
            "lines": po.lines,
        }
    )
    return {
        "id": row.id,
        "po_number": row.po_number,
        "vendor_id": row.vendor_id,
        "po_date": row.po_date,
        "total": str(row.total),
        "lines": row.lines(),
    }


@router.get("/purchase-orders")
def list_purchase_orders(vendor_id: str | None = None):
    return [
        {
            "id": r.id,
            "po_number": r.po_number,
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "po_date": r.po_date,
            "total": str(r.total),
            "lines": r.lines(),
        }
        for r in store.list_pos(vendor_id=vendor_id)
    ]


@router.post("/goods-receipts", status_code=201)
def create_goods_receipt(gr: GoodsReceipt):
    row = store.create_gr(
        {
            "id": gr.id,
            "gr_number": gr.gr_number,
            "po_id": gr.po_id,
            "receipt_date": gr.receipt_date,
            "lines": [ln.model_dump(mode="json") for ln in gr.lines],
        }
    )
    return {
        "id": row.id,
        "gr_number": row.gr_number,
        "po_id": row.po_id,
        "receipt_date": row.receipt_date,
        "lines": row.lines(),
    }


@router.get("/goods-receipts")
def list_goods_receipts(po_id: str | None = None):
    if po_id:
        row = store.get_gr_by_po(po_id)
        rows = [row] if row is not None else []
    else:
        rows = store.list_grs()
    return [
        {
            "id": r.id,
            "gr_number": r.gr_number,
            "po_id": r.po_id,
            "receipt_date": r.receipt_date,
            "lines": r.lines(),
        }
        for r in rows
    ]


# -- ledger / payments ------------------------------------------------------


@router.get("/ledger", response_model=list[LedgerOut])
def list_ledger():
    return [
        LedgerOut(
            id=r.id,
            invoice_id=r.invoice_id,
            account=r.account,
            debit=r.debit,
            credit=r.credit,
            posted_at=r.posted_at.isoformat(),
        )
        for r in store.list_ledger()
    ]


@router.get("/payments", response_model=list[PaymentOut])
def list_payments():
    return [
        PaymentOut(
            id=r.id,
            invoice_id=r.invoice_id,
            amount=r.amount,
            scheduled_date=r.scheduled_date,
            status=r.status,
        )
        for r in store.list_payments()
    ]


# -- queue / aging / metrics / forecast / eval ------------------------------


@router.get("/queue/exceptions", response_model=list[InvoiceOut])
def exception_queue():
    flagged = store.list_invoices(status=InvoiceStatus.FLAGGED.value)
    on_hold = store.list_invoices(status=InvoiceStatus.ON_HOLD.value)
    return [_row_to_out(r) for r in flagged + on_hold]


@router.get("/aging", response_model=AgingOut)
def aging(as_of: str | None = None):
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    result = compute_aging(store.list_invoices(), as_of=as_of_date)
    return AgingOut(
        as_of=result["as_of"],
        buckets=result["buckets"],
        overdue_count=result["overdue_count"],
        overdue_total_inr=Decimal(result["overdue_total_inr"]),
        oldest_overdue_days=result["oldest_overdue_days"],
    )


@router.get("/metrics", response_model=MetricsOut)
def metrics():
    rows = store.list_invoices()
    by_status: dict[str, int] = {}
    money_in_exceptions = Decimal("0.00")
    matched_clean = 0
    matched_total = 0
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        if row.status in (InvoiceStatus.FLAGGED.value, InvoiceStatus.ON_HOLD.value):
            money_in_exceptions += Decimal(str(row.total))
        if row.match_json:
            matched_total += 1
            match = json.loads(row.match_json)
            if match.get("outcome") == "clean":
                matched_clean += 1
    # Auto-approvals are recorded as audit events by the graph sync.
    auto_approved = 0
    cycle_seconds: list[float] = []
    for row in rows:
        entries = store.audit_for_invoice(row.id)
        if any(e.action == "auto_approved" for e in entries):
            auto_approved += 1
        if row.status in ("posted", "scheduled", "paid", "rejected") and entries:
            start = entries[0].created_at
            end = entries[-1].created_at
            cycle_seconds.append((end - start).total_seconds())
    aging_result = compute_aging(rows)
    latest_eval = store.latest_eval_run("synthetic")
    false_rate = None
    if latest_eval is not None:
        false_rate = json.loads(latest_eval.metrics_json).get("false_approve_rate")
    return MetricsOut(
        total_invoices=len(rows),
        by_status=by_status,
        exceptions_open=by_status.get("flagged", 0) + by_status.get("on_hold", 0),
        money_in_exceptions_inr=money_in_exceptions,
        auto_approved=auto_approved,
        match_rate=matched_clean / matched_total if matched_total else 0.0,
        auto_approve_rate=auto_approved / len(rows) if rows else 0.0,
        false_approve_rate=false_rate,
        median_cycle_time_seconds=(
            statistics.median(cycle_seconds) if cycle_seconds else None
        ),
        overdue_count=aging_result["overdue_count"],
        overdue_total_inr=Decimal(aging_result["overdue_total_inr"]),
    )


@router.get("/forecast")
def forecast(
    weeks: int = 4,
    weekly_budget_inr: Decimal | None = None,
    cost_of_capital_pct: Decimal = Decimal("12"),
):
    invoices = [
        inv for inv in (_row_to_invoice(r) for r in store.list_invoices()) if inv
    ]
    service = ForecastService(cost_of_capital_annual_pct=cost_of_capital_pct)
    return service.forecast(
        invoices, weeks=weeks, weekly_budget_inr=weekly_budget_inr
    )


@router.post("/forecast/discount")
def forecast_discount(body: DiscountIn):
    row = store.get_invoice(body.invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    invoice = _row_to_invoice(row)
    if invoice is None:
        raise HTTPException(status_code=422, detail="stored invoice is malformed")
    rec = ForecastService().discount_recommendation_for_terms(invoice, body.terms)
    if rec is None:
        raise HTTPException(
            status_code=422,
            detail=f"could not parse payment terms {body.terms!r} "
            "(expected e.g. '2/10 Net 30')",
        )
    return rec


@router.post("/eval/run", response_model=EvalRunOut)
def run_eval(n_per_scenario: int = 2, include_graph: bool = True):
    result = run_synthetic_eval(n_per_scenario=n_per_scenario)
    store.save_eval_run("synthetic", result)
    graph_result = None
    if include_graph:
        graph_result = run_graph_eval(n_per_scenario=n_per_scenario)
        store.save_eval_run("graph", graph_result)
    return EvalRunOut(kind="synthetic", metrics=result, graph_eval=graph_result)
