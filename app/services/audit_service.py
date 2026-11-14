"""Audit service — append-only audit log (§2, §19a.5).

Every action lands here with actor + timestamp. The log is append-only:
no update or delete operations exist by design.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditEntry(BaseModel):
    id: int | None = None
    invoice_id: str | None = None
    actor: str  # "system", "auto", or a user id
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AuditService:
    """In-memory append-only log for local dev; the SQLAlchemy repository
    in infra/db.py persists the same entries in production."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(
        self, action: str, actor: str = "system", invoice_id: str | None = None, **detail: Any
    ) -> AuditEntry:
        entry = AuditEntry(
            invoice_id=invoice_id, actor=actor, action=action, detail=detail
        )
        self._entries.append(entry)
        return entry

    def for_invoice(self, invoice_id: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.invoice_id == invoice_id]

    def all(self) -> list[AuditEntry]:
        return list(self._entries)
