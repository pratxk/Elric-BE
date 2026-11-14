"""Graph tests — StateGraph routing with a pre-extracted perfect invoice.

The extractor is bypassed by seeding `extracted` in state (the real
extractor is stubbed in the scaffold and needs a Gemini key).
"""

from app.domain.models import MatchOutcome
from app.eval.synthetic import generate_corpus
from app.graph import build_graph


def _extracted_state(case: dict) -> dict:
    return {
        "invoice_id": case["invoice"].id,
        "document_hash": "test-hash",
        "extracted": {
            "invoice": case["invoice"].model_dump(mode="json"),
            "po": case["po"].model_dump(mode="json") if case["po"] else None,
            "gr": case["gr"].model_dump(mode="json") if case["gr"] else None,
            "existing_invoices": [
                i.model_dump(mode="json") for i in case["existing"]
            ],
            "buyer_state": case["buyer_state"],
            "seller_state": case["seller_state"],
            "total": str(case["invoice"].total),
        },
        "audit": [],
    }


def test_graph_auto_approves_perfect_invoice():
    case = next(
        c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == "perfect"
    )
    graph = build_graph()
    result = graph.invoke(
        _extracted_state(case),
        config={"configurable": {"thread_id": f"test-{case['invoice'].id}"}},
    )
    assert result["match"]["outcome"] == MatchOutcome.CLEAN.value
    assert result["auto_approved"] is True
    assert result["status"] == "posted"  # booked after auto-approve
