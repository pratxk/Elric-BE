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
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
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
    user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
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
    # Last extraction failure, surfaced verbatim to the UI so the user
    # sees the TRUE reason (e.g. Gemini 503 overload) instead of a
    # guessed "no key configured". Cleared on the next successful run.
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_error_kind: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # "no_key" | "overloaded" | "quota" | "failed"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def lines(self) -> list[dict]:
        return json.loads(self.lines_json or "[]")

    def exceptions(self) -> list[dict]:
        return json.loads(self.exceptions_json or "[]")


class AuditRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
    user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    invoice_id: Mapped[str] = mapped_column(String, index=True)
    account: Mapped[str] = mapped_column(String)  # GL code, e.g. "AP-2000"
    debit: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    credit: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PaymentRow(Base):
    """Scheduled (never executed) payments (§19a.5)."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    invoice_id: Mapped[str] = mapped_column(String, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    scheduled_date: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="scheduled")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PurchaseOrderRow(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
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
    user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    gr_number: Mapped[str] = mapped_column(String)
    po_id: Mapped[str] = mapped_column(String, index=True)
    receipt_date: Mapped[str] = mapped_column(String, default="")
    lines_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def lines(self) -> list[dict]:
        return json.loads(self.lines_json or "[]")


class UserRow(Base):
    """Auth users (v1.1, email + password). Passwords are bcrypt hashes
    only — plaintext is never stored, logged, or returned."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RefreshTokenRow(Base):
    """Opaque refresh tokens (v1.1). Only the SHA-256 hash is stored —
    the raw token is never persisted. One row per login session lineage
    step; rotation revokes the old row and issues a new one in the same
    family_id. A revoked row presented again is reuse → whole family
    revoked (see app/services/auth_service.py)."""

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    family_id: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LoginAttemptRow(Base):
    """Failed login attempts, DB-backed so the lockout survives restarts.
    Only failures are recorded; a successful login clears the email's rows."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SettingsRow(Base):
    """One row per user (v1.1). Defaults are applied on first read —
    see app/infra/store.py::get_or_create_settings."""

    __tablename__ = "settings"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    company_name: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str] = mapped_column(String, default="IN")
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="INR")
    auto_approve_limit_inr: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("500000")
    )
    tolerance_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=Decimal("2.0")
    )
    tolerance_flat_inr: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("500")
    )
    duplicate_window_days: Mapped[int] = mapped_column(Integer, default=90)


class VendorRow(Base):
    """Per-user vendor registry (v1.1). Vendors are never seeded and
    never global — every row belongs to exactly one user."""

    __tablename__ = "vendors"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    gstin: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    # v1.1 fix round: the extraction failure is persisted on the row so
    # the API can surface the true reason (503 overload, quota, no key)
    # instead of the UI guessing "no key configured".
    ("invoices", "extraction_error", "TEXT"),
    ("invoices", "extraction_error_kind", "VARCHAR"),
    # v1.1 per-user scoping: legacy rows keep user_id NULL and are
    # invisible to every user (never served by any endpoint).
    ("invoices", "user_id", "VARCHAR"),
    ("purchase_orders", "user_id", "VARCHAR"),
    ("goods_receipts", "user_id", "VARCHAR"),
    ("ledger_entries", "user_id", "VARCHAR"),
    ("payments", "user_id", "VARCHAR"),
    ("audit_log", "user_id", "VARCHAR"),
]


def migrate(engine) -> list[str]:
    """Add missing columns. Returns the list of columns added (empty when
    the DB is already current). Safe to call on every boot."""
    added: list[str] = []
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "sqlite":
            tables = {
                r[0]
                for r in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        else:  # postgres and friends
            tables = {
                r[0]
                for r in conn.exec_driver_sql(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ).fetchall()
            }
        for table, column, ddl_type in _MIGRATIONS:
            if table not in tables:
                # A genuinely old DB may predate the table itself (the
                # migration test builds an invoices-only DB); create_all
                # owns table creation, migrate only adds columns.
                continue
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
