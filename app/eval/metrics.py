"""Eval metrics (§9).

Two levels, both on the seeded synthetic corpus:
- Matcher eval: the deterministic matcher directly — match rate,
  auto-approve rate, false-approve rate (target 0), per-scenario code
  accuracy.
- Graph eval: the same corpus run END-TO-END through the LangGraph
  (extraction mocked by seeding ``extracted`` in state, as in the graph
  tests — no LLM calls). Reports final statuses, auto-approve rate,
  false-approve rate (posted/approved despite expected exceptions) and
  exception-code accuracy through the full pipeline.
"""

from __future__ import annotations

from app.domain.models import MatchOutcome
from app.domain.policies import AutoApprovePolicy, auto_approve_decision
from app.eval.synthetic import generate_corpus
from app.services.matching_service import MatchingService


def run_synthetic_eval(n_per_scenario: int = 2, seed: int = 42) -> dict:
    matcher = MatchingService()
    policy = AutoApprovePolicy()
    cases = generate_corpus(n_per_scenario=n_per_scenario, seed=seed)

    total = len(cases)
    matched_clean = 0
    auto_approved = 0
    false_approves = 0
    code_correct = 0
    per_scenario: dict[str, dict] = {}

    for case in cases:
        match = matcher.match(
            case["invoice"],
            case["po"],
            case["gr"],
            existing_invoices=case["existing"],
            buyer_state_code=case["buyer_state"],
            seller_state_code=case["seller_state"],
        )
        expected = set(case["expected_codes"])
        got = {c.value for c in match.exception_codes}
        correct = expected == got
        code_correct += int(correct)
        if match.outcome == MatchOutcome.CLEAN:
            matched_clean += 1

        approved, _ = auto_approve_decision(match, case["invoice"].total, policy)
        auto_approved += int(approved)
        # A false approve = auto-approved despite expected exceptions.
        if approved and expected:
            false_approves += 1

        entry = per_scenario.setdefault(
            case["scenario"], {"n": 0, "code_correct": 0, "auto_approved": 0}
        )
        entry["n"] += 1
        entry["code_correct"] += int(correct)
        entry["auto_approved"] += int(approved)

    return {
        "kind": "synthetic",
        "n_cases": total,
        "match_rate": matched_clean / total if total else 0.0,
        "auto_approve_rate": auto_approved / total if total else 0.0,
        "false_approve_rate": false_approves / total if total else 0.0,
        "false_approves": false_approves,
        "exception_code_accuracy": code_correct / total if total else 0.0,
        "per_scenario": per_scenario,
    }


def run_graph_eval(n_per_scenario: int = 2, seed: int = 42) -> dict:
    """Run the synthetic corpus end-to-end through the LangGraph.

    Extraction is mocked by seeding ``extracted`` in state (the extractor
    node passes seeded extraction through untouched) — no LLM calls.
    A case counts as a false approve when the run ends posted/approved
    (or auto-approved) despite expected exception codes.
    """
    from app.graph import build_graph

    graph = build_graph()
    cases = generate_corpus(n_per_scenario=n_per_scenario, seed=seed)
    total = len(cases)
    auto_approved = 0
    false_approves = 0
    code_correct = 0
    posted = 0
    flagged = 0
    per_scenario: dict[str, dict] = {}
    for case in cases:
        thread_id = f"eval-{case['invoice'].id}-{seed}"
        result = graph.invoke(
            {
                "invoice_id": case["invoice"].id,
                "document_hash": "eval-hash",
                "vendor_name": "Acme Corporation Pvt Ltd",
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
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        expected = set(case["expected_codes"])
        got = {
            c.value if hasattr(c, "value") else str(c)
            for c in (result.get("match") or {}).get("exception_codes", [])
        }
        correct = expected == got
        code_correct += int(correct)
        approved = bool(result.get("auto_approved")) or result.get("status") in (
            "approved",
            "posted",
        )
        auto_approved += int(bool(result.get("auto_approved")))
        if result.get("status") == "posted":
            posted += 1
        if result.get("__interrupt__") or result.get("status") == "flagged":
            flagged += 1
        if approved and expected:
            false_approves += 1
        entry = per_scenario.setdefault(
            case["scenario"], {"n": 0, "code_correct": 0, "posted": 0, "flagged": 0}
        )
        entry["n"] += 1
        entry["code_correct"] += int(correct)
        entry["posted"] += int(result.get("status") == "posted")
        entry["flagged"] += int(
            bool(result.get("__interrupt__")) or result.get("status") == "flagged"
        )
    return {
        "kind": "graph",
        "n_cases": total,
        "auto_approve_rate": auto_approved / total if total else 0.0,
        "false_approve_rate": false_approves / total if total else 0.0,
        "false_approves": false_approves,
        "exception_code_accuracy": code_correct / total if total else 0.0,
        "posted": posted,
        "flagged": flagged,
        "per_scenario": per_scenario,
    }
