"""Ingestor node — intake, file validation, hash dedupe."""

from __future__ import annotations

import hashlib

from app.agents.state import InvoiceState


def ingestor_node(state: InvoiceState) -> dict:
    # In the scaffold the document bytes arrive via state; the API computes
    # the hash at upload time. Here we just record the intake event.
    doc_hash = state.get("document_hash")
    return {
        "status": "received",
        "audit": [f"ingestor: received document hash={doc_hash}"],
    }


def document_hash(document_bytes: bytes) -> str:
    return hashlib.sha256(document_bytes).hexdigest()
