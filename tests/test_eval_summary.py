"""GET /api/eval/summary tests (v1.1).

Contract: {"synthetic": {...}|null, "graph": {...}|null,
"generated_at": "<ISO>", "cached": true|false}. Cached stored runs are
returned with cached=true; with no stored runs both evals run now
(n_per_scenario=2, seed=42) and cached=false; ?refresh=true forces a
re-run. Zero LLM calls (the harness is synthetic + seeded-graph only).
"""

from __future__ import annotations

from app.eval.metrics import run_graph_eval, run_synthetic_eval
from tests.helpers import make_client, make_plain_client


def test_eval_summary_runs_and_caches():
    client = make_client()
    first = client.get("/api/eval/summary")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["cached"] is False
    assert body["synthetic"]["n_cases"] == 14
    assert body["synthetic"]["false_approve_rate"] == 0.0
    assert body["graph"]["n_cases"] == 14
    assert body["graph"]["false_approve_rate"] == 0.0
    assert body["generated_at"]
    # Second call serves the stored runs.
    second = client.get("/api/eval/summary")
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert second.json()["synthetic"] == body["synthetic"]
    assert second.json()["graph"] == body["graph"]


def test_eval_summary_matches_harness_output():
    client = make_client()
    body = client.get("/api/eval/summary").json()
    assert body["synthetic"] == run_synthetic_eval(n_per_scenario=2, seed=42)
    assert body["graph"] == run_graph_eval(n_per_scenario=2, seed=42)


def test_eval_summary_refresh_forces_rerun():
    client = make_client()
    assert client.get("/api/eval/summary").json()["cached"] is False
    assert client.get("/api/eval/summary").json()["cached"] is True
    refreshed = client.get("/api/eval/summary", params={"refresh": "true"})
    assert refreshed.status_code == 200
    assert refreshed.json()["cached"] is False
    assert refreshed.json()["synthetic"]["n_cases"] == 14


def test_eval_summary_401_without_auth():
    assert make_plain_client().get("/api/eval/summary").status_code == 401
