"""LangGraph StateGraph wiring (§4).

Flow: ingest → extract → validate → match → (clean ? auto-approve path
: exception path) → human review (interrupt) → book/post → done.

Conditional edges on match outcome and amount threshold. Checkpointer:
Postgres when DATABASE_URL is set, in-memory fallback for local dev.
"""

from __future__ import annotations

import logging
import os

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.approver import approver_node
from app.agents.auditor import auditor_node
from app.agents.bookkeeper import bookkeeper_node
from app.agents.exceptions import exceptions_node
from app.agents.extractor import extractor_node
from app.agents.ingestor import ingestor_node
from app.agents.matcher import matcher_node
from app.agents.state import InvoiceState
from app.agents.supervisor import (
    route_after_approval,
    route_after_extract,
    route_after_match,
    supervisor_node,
)
from app.agents.validate import validate_node

logger = logging.getLogger(__name__)


def build_checkpointer():
    """Postgres checkpointer when DATABASE_URL is set; MemorySaver otherwise."""
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            saver = PostgresSaver.from_conn_string(database_url)
            saver.setup()
            logger.info("using Postgres checkpointer")
            return saver
        except Exception as exc:  # noqa: BLE001 — fall back, don't crash boot
            logger.warning("Postgres checkpointer unavailable (%s); using memory", exc)
    return MemorySaver()


def build_graph():
    graph = StateGraph(InvoiceState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("ingest", ingestor_node)
    graph.add_node("extract", extractor_node)
    graph.add_node("validate", validate_node)
    graph.add_node("match", matcher_node)
    graph.add_node("exception", exceptions_node)
    graph.add_node("approve", approver_node)
    graph.add_node("book", bookkeeper_node)
    graph.add_node("audit", auditor_node)

    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", "ingest")
    graph.add_edge("ingest", "extract")
    graph.add_conditional_edges(
        "extract", route_after_extract, {"validate": "validate", "audit": "audit"}
    )
    graph.add_edge("validate", "match")
    graph.add_conditional_edges(
        "match", route_after_match, {"book": "book", "exception": "exception"}
    )
    graph.add_edge("exception", "approve")
    graph.add_conditional_edges(
        "approve", route_after_approval, {"book": "book", "done": "audit", "exception": "exception"}
    )
    graph.add_edge("book", "audit")
    graph.add_edge("audit", END)
    return graph.compile(checkpointer=build_checkpointer())


# Built lazily on first use so importing the module never touches the DB.
_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
