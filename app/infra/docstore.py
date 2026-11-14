"""In-process document registry.

Upload stores the raw document bytes here keyed by invoice id; the
extractor node reads them back. In-process only by design (v1 demo):
Render disk is ephemeral, so production file storage is object storage
(R2, §19f) — this registry is the seam where that plugs in.
"""

from __future__ import annotations

_DOCUMENTS: dict[str, bytes] = {}


def register(invoice_id: str, document_bytes: bytes) -> None:
    _DOCUMENTS[invoice_id] = document_bytes


def get(invoice_id: str) -> bytes | None:
    return _DOCUMENTS.get(invoice_id)


def drop(invoice_id: str) -> None:
    _DOCUMENTS.pop(invoice_id, None)


def clear() -> None:
    _DOCUMENTS.clear()
