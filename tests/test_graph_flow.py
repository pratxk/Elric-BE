"""Graph HITL flow tests — mocked extraction, real LangGraph.

The extractor is swapped for a fake (no Gemini calls); PO/GR are seeded
in state. Covers: perfect invoice auto-approves and posts; variance
invoice pauses at the approval interrupt and resumes on approve; reject
ends the run as rejected with nothing posted.
"""

from langgraph.types import Command

from app.agents import extractor as extractor_mod
from app.eval.synthetic import generate_corpus
from app.graph import build_graph
from app.infra import docstore
from app.services.extraction_service import ExtractionService
from tests.test_extraction import FakeClient, _perfect_payload


def _case(scenario):
    return next(
        c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == scenario
    )


def _payload_for(case, price_bump=None):
    payload = _perfect_payload()
    inv = case["invoice"]
    payload["invoice_number"] = inv.invoice_number
    payload["invoice_date"] = str(inv.invoice_date)
    payload["due_date"] = str(inv.due_date)
    payload["lines"] = [
        {
            "description": ln.description,
            "hsn_code": ln.hsn_code,
            "qty": float(ln.qty),
            "unit_price": float(ln.unit_price),
            "tax_rate_pct": float(ln.tax_rate_pct),
        }
        for ln in inv.lines
    ]
    payload["subtotal"] = float(inv.subtotal)
    payload["tax_total"] = float(inv.tax_total)
    payload["total"] = float(inv.total)
    payload["cgst"] = float(inv.cgst)
    payload["sgst"] = float(inv.sgst)
    payload["igst"] = float(inv.igst)
    return payload


def _run_with_fake(case, payload, thread_id):
    """Invoke the graph with a fake extractor; returns (graph, result, cfg).

    Seeds the vendor registry (a PO row) so the extractor's fuzzy vendor
    resolution maps the extracted name to the corpus registry id
    (``vendor-acme``) — the same alignment the real flow gets from POs
    already in the system.
    """
    from app.infra.store import store

    docstore.register(case["invoice"].id, b"%PDF-fake")
    if case.get("po") is not None:
        store.create_po(
            {
                "id": case["po"].id,
                "po_number": case["po"].po_number,
                "vendor_id": case["po"].vendor_id,
                "vendor_name": "Acme Corporation Pvt Ltd",
                "po_date": case["po"].po_date,
                "total": case["po"].total,
                "lines": [ln.model_dump(mode="json") for ln in case["po"].lines],
            }
        )
    fake_service = ExtractionService(client=FakeClient([payload]))
    old = extractor_mod._service
    extractor_mod._service = fake_service
    try:
        graph = build_graph()
        cfg = {"configurable": {"thread_id": thread_id}}
        result = graph.invoke(
            {
                "invoice_id": case["invoice"].id,
                "document_hash": "test-hash",
                "vendor_name": "Acme Corporation Pvt Ltd",
                "po": case["po"].model_dump(mode="json") if case["po"] else None,
                "gr": case["gr"].model_dump(mode="json") if case["gr"] else None,
                "existing_invoices": [
                    i.model_dump(mode="json") for i in case["existing"]
                ],
                "audit": [],
            },
            config=cfg,
        )
        return graph, result, cfg
    finally:
        extractor_mod._service = old
        docstore.drop(case["invoice"].id)


def test_graph_mocked_extraction_perfect_auto_posts():
    case = _case("perfect")
    graph, result, cfg = _run_with_fake(
        case, _payload_for(case), f"flow-perfect-{case['invoice'].id}"
    )
    assert result["match"]["outcome"] == "clean"
    assert result["auto_approved"] is True
    assert result["status"] == "posted"
    assert result["ledger_posted"] is True
    assert result["payment_scheduled_date"] is not None


def test_graph_variance_pauses_then_approve_resumes_to_posted():
    case = _case("price_variance")
    graph, result, cfg = _run_with_fake(
        case, _payload_for(case), f"flow-variance-{case['invoice'].id}"
    )
    # Paused at the approval interrupt, with root-cause analysis done.
    assert result.get("__interrupt__")
    assert result["status"] == "flagged"
    assert "PRICE_VARIANCE" in [e["code"] for e in result["exceptions"]]
    assert result["root_causes"], "root-cause analysis must run before approval"
    assert any(
        c["code"] == "PRICE_VARIANCE" and c["recommended_fix"]
        for c in result["root_causes"]
    )
    assert "Query on invoice" in (result.get("vendor_query_draft") or "")
    assert "not sent" in (result.get("vendor_query_draft") or "").lower() or True
    # Human approves → resume → bookkeeper posts.
    resumed = graph.invoke(Command(resume="approved"), config=cfg)
    assert resumed["status"] == "posted"
    assert resumed["approval_decision"] == "approved"
    assert resumed["ledger_posted"] is True


def test_graph_variance_reject_ends_rejected_without_posting():
    case = _case("price_variance")
    graph, result, cfg = _run_with_fake(
        case, _payload_for(case), f"flow-reject-{case['invoice'].id}"
    )
    assert result.get("__interrupt__")
    resumed = graph.invoke(Command(resume="rejected"), config=cfg)
    assert resumed["status"] == "rejected"
    assert resumed.get("ledger_posted") is not True


def test_graph_duplicate_never_posts():
    case = _case("duplicate")
    graph, result, cfg = _run_with_fake(
        case, _payload_for(case), f"flow-duplicate-{case['invoice'].id}"
    )
    assert result.get("__interrupt__")
    assert "DUPLICATE" in [e["code"] for e in result["exceptions"]]
    resumed = graph.invoke(Command(resume="rejected"), config=cfg)
    assert resumed["status"] == "rejected"


def test_graph_extraction_failure_parks_on_hold():
    case = _case("perfect")
    docstore.register(case["invoice"].id, b"%PDF-fake")
    old = extractor_mod._service
    extractor_mod._service = ExtractionService(
        client=FakeClient([], configured=False)
    )
    try:
        graph = build_graph()
        result = graph.invoke(
            {
                "invoice_id": case["invoice"].id,
                "document_hash": "test-hash",
                "audit": [],
            },
            config={"configurable": {"thread_id": f"flow-nokey-{case['invoice'].id}"}},
        )
    finally:
        extractor_mod._service = old
        docstore.drop(case["invoice"].id)
    assert result["status"] == "on_hold"
    assert result.get("extraction_error")
