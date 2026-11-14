"""Migration tests (§16 coordinator note): a shipped elric.db predating
a column must never 500 — init_db/migrate adds missing columns in place.

The test builds a genuinely OLD-schema invoices table (no payment_terms,
no extraction_json), runs migrate() against it, and asserts the columns
appear and the migration is idempotent.
"""

import os

import pytest
from sqlalchemy import create_engine, text

from app.infra import db as db_mod

OLD_SCHEMA = """
CREATE TABLE invoices (
    id VARCHAR NOT NULL PRIMARY KEY,
    vendor_id VARCHAR NOT NULL,
    vendor_name VARCHAR NOT NULL,
    invoice_number VARCHAR NOT NULL,
    invoice_date VARCHAR NOT NULL,
    due_date VARCHAR,
    currency VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    subtotal NUMERIC(14, 2) NOT NULL,
    tax_total NUMERIC(14, 2) NOT NULL,
    total NUMERIC(14, 2) NOT NULL,
    document_hash VARCHAR,
    lines_json TEXT NOT NULL,
    match_json TEXT,
    exceptions_json TEXT NOT NULL,
    created_at DATETIME NOT NULL
)
"""


def _columns(engine):
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(invoices)").fetchall()
    return {r[1] for r in rows}


def test_migrate_adds_missing_columns_to_old_db(tmp_path):
    path = tmp_path / "old-elric.db"
    import sqlite3

    sqlite3.connect(str(path)).executescript(OLD_SCHEMA).close()
    engine = create_engine(f"sqlite:///{path}")
    assert "payment_terms" not in _columns(engine)
    added = db_mod.migrate(engine)
    assert "invoices.payment_terms" in added
    assert "invoices.extraction_json" in added
    # v1.1 fix round: the extraction failure is persisted on the row so
    # the API can surface the true reason (503 overload, quota, no key).
    assert "invoices.extraction_error" in added
    assert "invoices.extraction_error_kind" in added
    # v1.1 per-user scoping column lands on old DBs too. The other
    # user_id columns belong to tables this fixture DB predates
    # (purchase_orders etc. don't exist here) — migrate skips missing
    # tables by design; create_all owns table creation.
    assert "invoices.user_id" in added
    cols = _columns(engine)
    assert "payment_terms" in cols and "extraction_json" in cols
    # Idempotent: a second run adds nothing.
    assert db_mod.migrate(engine) == []


def test_migrate_is_noop_on_current_db():
    engine = db_mod._engine
    assert db_mod.migrate(engine) == []
    cols = _columns(engine)
    assert "payment_terms" in cols and "extraction_json" in cols
