"""SQLAlchemy persistence (§7).

Postgres via DATABASE_URL (Neon-ready); SQLite file for local dev.
Tables mirror the domain ontology; the API stores invoice records as
JSON columns for lines/matches so the scaffold stays simple.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL") or "sqlite:///./elric.db"

_engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class InvoiceRow(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    vendor_id: Mapped[str] = mapped_column(String, default="")
    vendor_name: Mapped[str] = mapped_column(String, default="")
    invoice_number: Mapped[str] = mapped_column(String, default="")
    invoice_date: Mapped[str] = mapped_column(String, default="")
    due_date: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="INR")
    status: Mapped[str] = mapped_column(String, default="received")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    document_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    lines_json: Mapped[str] = mapped_column(Text, default="[]")
    match_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    exceptions_json: Mapped[str] = mapped_column(Text, default="[]")
    payment_terms: Mapped[str | None] = mapped_column(String, nullable=True)
    extraction_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def lines(self) -> list[dict]:
        return json.loads(self.lines_json or "[]")

    def exceptions(self) -> list[dict]:
        return json.loads(self.exceptions_json or "[]")


class AuditRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    invoice_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String, default="system")
    action: Mapped[str] = mapped_column(String)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EvalRunRow(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, default="synthetic")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LedgerRow(Base):
    """Bookkeeper postings (§3). Balanced double-entry: Dr purchases,
    Cr accounts payable, one pair per approved invoice."""

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String, index=True)
    account: Mapped[str] = mapped_column(String)  # GL code, e.g. "AP-2000"
    debit: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    credit: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PaymentRow(Base):
    """Scheduled (never executed) payments (§19a.5)."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    scheduled_date: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="scheduled")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PurchaseOrderRow(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    po_number: Mapped[str] = mapped_column(String)
    vendor_id: Mapped[str] = mapped_column(String, default="")
    vendor_name: Mapped[str] = mapped_column(String, default="")
    po_date: Mapped[str] = mapped_column(String, default="")
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    lines_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def lines(self) -> list[dict]:
        return json.loads(self.lines_json or "[]")


class GoodsReceiptRow(Base):
    __tablename__ = "goods_receipts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    gr_number: Mapped[str] = mapped_column(String)
    po_id: Mapped[str] = mapped_column(String, index=True)
    receipt_date: Mapped[str] = mapped_column(String, default="")
    lines_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def lines(self) -> list[dict]:
        return json.loads(self.lines_json or "[]")


def init_db() -> None:
    Base.metadata.create_all(bind=_engine)
    migrate(_engine)


# -- lightweight migrations ----------------------------------------------
# create_all() only creates MISSING tables; it never adds a column to an
# existing table. A shipped elric.db predating a new column therefore 500s
# on every invoice query ("no such column") until someone ALTERs it by
# hand — that is the drift the v1 migration fixes. Every column added to
# a model gets an entry here, so a clean checkout AND an old DB both boot.
# Idempotent: already-present columns are skipped. Add-only: no column is
# ever dropped or renamed by this path.
_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, DDL type)
    ("invoices", "payment_terms", "VARCHAR"),
    ("invoices", "extraction_json", "TEXT"),
]


def migrate(engine) -> list[str]:
    """Add missing columns. Returns the list of columns added (empty when
    the DB is already current). Safe to call on every boot."""
    added: list[str] = []
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for table, column, ddl_type in _MIGRATIONS:
            if dialect == "sqlite":
                rows = conn.exec_driver_sql(
                    f"PRAGMA table_info({table})"
                ).fetchall()
                existing = {r[1] for r in rows}
            else:  # postgres and friends
                rows = conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s",
                    (table,),
                ).fetchall()
                existing = {r[0] for r in rows}
            if column not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
                )
                added.append(f"{table}.{column}")
    return added


def get_session():
    return SessionLocal()
