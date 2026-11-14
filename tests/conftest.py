"""Test isolation for the Elric backend.

- Tests run against a throwaway SQLite file in /tmp (never the dev
  ``elric.db``), so verification numbers stay clean and the dev DB keeps
  no test residue.
- Gemini keys are forcibly unset: no test may ever make a real LLM call
  (quota rule — "dont run out both keys tho"). Extraction tests inject a
  fake client; the graph tests seed extraction or inject a fake service.
- Every test starts from an empty store (tables dropped + recreated) and
  an empty document registry, so tests never see each other's invoices,
  ledger entries or eval runs — synthetic ids (e.g. ``inv-43``) are
  reused across tests and must not collide.

This file is imported by pytest before any test module imports ``app``,
so the DATABASE_URL set here is the one ``app.infra.db`` binds to.
"""

from __future__ import annotations

import os

import pytest

os.environ["DATABASE_URL"] = f"sqlite:////tmp/elric-test-{os.getpid()}.db"
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GEMINI_API_KEY_SECONDARY", None)

_db_path = f"/tmp/elric-test-{os.getpid()}.db"
for suffix in ("", "-journal"):
    try:
        os.remove(_db_path + suffix)
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def _isolated_store():
    """Fresh store + empty doc registry for every test."""
    from app.infra import db as db_mod
    from app.infra import docstore

    db_mod.Base.metadata.drop_all(bind=db_mod._engine)
    db_mod.Base.metadata.create_all(bind=db_mod._engine)
    db_mod.migrate(db_mod._engine)
    docstore.clear()
    yield
    docstore.clear()
