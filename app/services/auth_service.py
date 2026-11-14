"""Auth service (v1.1) — email + password login, access + refresh tokens.

Password hashing: **bcrypt, cost 12**, via the `bcrypt` package directly
(passlib is unmaintained and breaks with bcrypt 4+; the raw package is
the maintained path). Plaintext passwords are never stored, logged, or
returned; hashes never leave the server in any API response.

Token contract (pinned, see backend/README.md):
- Access token: JWT (HS256, 15 min), returned as `access_token` in JSON,
  sent as `Authorization: Bearer <token>`.
- Refresh token: opaque random token (7 days), stored only as a SHA-256
  hash, delivered in the HttpOnly `elric_refresh` cookie. Rotation issues
  a new token in the same family_id and revokes the old one; presenting
  an already-rotated (revoked) or expired token revokes the WHOLE family
  (reuse detection) and forces re-login. A completely unknown token hash
  revokes nothing — it may be a stale cookie — and just returns 401.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

import bcrypt

from app.config import settings
from app.infra import db
from app.infra.db import LoginAttemptRow, RefreshTokenRow, UserRow
from app.infra.jwt import decode_access_token, encode_access_token

# -- password policy ------------------------------------------------------
# Minimum 12 chars, at least one upper, one lower, one digit, one symbol.
# Max 72 chars: bcrypt silently truncates (older versions) or raises
# (bcrypt 5+) beyond 72 bytes, so over-long passwords are rejected
# outright rather than hashed truncated.
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 72
BCRYPT_COST = 12

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SYMBOL_RE = re.compile(r"[^A-Za-z0-9]")

REFRESH_TOKEN_TTL = timedelta(days=7)
MAX_FAILED_LOGINS = 5
LOCKOUT_WINDOW = timedelta(minutes=15)


class DuplicateEmailError(ValueError):
    """Register attempted with an email that already has an account."""


class InvalidPasswordError(ValueError):
    """Current password did not match (change-password path)."""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    """Returns the normalized email, or raises ValueError (→ 422)."""
    normalized = normalize_email(email)
    if not _EMAIL_RE.match(normalized):
        raise ValueError("invalid email address")
    return normalized


def validate_password(password: str) -> None:
    """Enforce the password policy. Raises ValueError with a clear
    message (→ 422). Never logs or echoes the password."""
    if not isinstance(password, str) or not password:
        raise ValueError("password is required")
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LEN:
        problems.append(f"at least {MIN_PASSWORD_LEN} characters")
    if len(password) > MAX_PASSWORD_LEN:
        problems.append(f"at most {MAX_PASSWORD_LEN} characters")
    if not any(c.isupper() for c in password):
        problems.append("one uppercase letter")
    if not any(c.islower() for c in password):
        problems.append("one lowercase letter")
    if not any(c.isdigit() for c in password):
        problems.append("one digit")
    if not _SYMBOL_RE.search(password):
        problems.append("one symbol")
    if problems:
        raise ValueError(
            "password must contain " + ", ".join(problems)
        )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_COST)
    ).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("ascii")
        )
    except Exception:  # noqa: BLE001 — a malformed hash is just "no match"
        return False


# -- users -----------------------------------------------------------------

def get_user(user_id: str) -> UserRow | None:
    session = db.get_session()
    try:
        return session.get(UserRow, user_id)
    finally:
        session.close()


def get_user_by_email(email: str) -> UserRow | None:
    session = db.get_session()
    try:
        return (
            session.query(UserRow)
            .filter(UserRow.email == normalize_email(email))
            .first()
        )
    finally:
        session.close()


def create_user(email: str, password: str) -> UserRow:
    """Create a user. Raises DuplicateEmailError / ValueError (policy)."""
    email = validate_email(email)
    validate_password(password)
    if get_user_by_email(email) is not None:
        raise DuplicateEmailError(
            "an account with this email already exists"
        )
    session = db.get_session()
    try:
        row = UserRow(
            id=str(uuid4()),
            email=email,
            password_hash=hash_password(password),
            onboarding_completed=False,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    finally:
        session.close()


def verify_user_password(user: UserRow, password: str) -> bool:
    return verify_password(password, user.password_hash)


def change_user_password(user_id: str, current_password: str, new_password: str) -> UserRow:
    """Change password; requires the current password. Raises
    InvalidPasswordError (→ 401) or ValueError (policy → 422)."""
    validate_password(new_password)
    session = db.get_session()
    try:
        row = session.get(UserRow, user_id)
        if row is None:
            raise InvalidPasswordError("user not found")
        if not verify_password(current_password, row.password_hash):
            raise InvalidPasswordError("current password is incorrect")
        row.password_hash = hash_password(new_password)
        session.commit()
        session.refresh(row)
        return row
    finally:
        session.close()


def complete_onboarding(user_id: str) -> UserRow | None:
    session = db.get_session()
    try:
        row = session.get(UserRow, user_id)
        if row is None:
            return None
        row.onboarding_completed = True
        session.commit()
        session.refresh(row)
        return row
    finally:
        session.close()


# -- login lockout (DB-backed, survives restart) ---------------------------

def record_failed_attempt(email: str) -> None:
    session = db.get_session()
    try:
        session.add(LoginAttemptRow(email=normalize_email(email)))
        session.commit()
    finally:
        session.close()


def clear_failed_attempts(email: str) -> None:
    session = db.get_session()
    try:
        session.query(LoginAttemptRow).filter(
            LoginAttemptRow.email == normalize_email(email)
        ).delete()
        session.commit()
    finally:
        session.close()


def failed_attempts_in_window(email: str) -> int:
    since = datetime.utcnow() - LOCKOUT_WINDOW
    session = db.get_session()
    try:
        return (
            session.query(LoginAttemptRow)
            .filter(
                LoginAttemptRow.email == normalize_email(email),
                LoginAttemptRow.created_at >= since,
            )
            .count()
        )
    finally:
        session.close()


def is_locked_out(email: str) -> bool:
    return failed_attempts_in_window(email) >= MAX_FAILED_LOGINS


# -- access tokens ----------------------------------------------------------

def issue_access_token(user: UserRow) -> str:
    return encode_access_token(user.id, user.email)


def verify_access_token(token: str) -> dict | None:
    return decode_access_token(token)


# -- refresh tokens ----------------------------------------------------------

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def create_refresh_token(user_id: str, family_id: str | None = None) -> tuple[str, RefreshTokenRow]:
    """Issue a refresh token in a (new or existing) family. Returns
    (raw_token, row). The raw token is returned once, to be set as a
    cookie — it is never stored."""
    raw = secrets.token_urlsafe(32)
    session = db.get_session()
    try:
        row = RefreshTokenRow(
            id=str(uuid4()),
            user_id=user_id,
            token_hash=_hash_token(raw),
            family_id=family_id or str(uuid4()),
            expires_at=datetime.utcnow() + REFRESH_TOKEN_TTL,
            revoked_at=None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return raw, row
    finally:
        session.close()


def _revoke_family(session, family_id: str) -> None:
    now = datetime.utcnow()
    session.query(RefreshTokenRow).filter(
        RefreshTokenRow.family_id == family_id,
        RefreshTokenRow.revoked_at.is_(None),
    ).update({"revoked_at": now})


def rotate_refresh_token(raw: str) -> tuple[str, str | None, RefreshTokenRow | None]:
    """Rotate a presented refresh token.

    Returns (status, new_raw_token, new_row):
    - ("ok", new_raw, new_row): valid token — old row revoked, new token
      issued in the SAME family_id.
    - ("reuse", None, None): the presented token was already revoked
      (i.e. rotated before — its successor was already used) or expired.
      The WHOLE family is revoked: every session in that lineage must
      re-login. This is the theft/replay detector.
    - ("unknown", None, None): no row matches the hash — a stale cookie
      or a forged token. Nothing is revoked; just 401.
    """
    token_hash = _hash_token(raw)
    session = db.get_session()
    try:
        row = (
            session.query(RefreshTokenRow)
            .filter(RefreshTokenRow.token_hash == token_hash)
            .first()
        )
        if row is None:
            return "unknown", None, None
        now = datetime.utcnow()
        if row.revoked_at is not None or row.expires_at <= now:
            # Revoked (already rotated — successor used) or expired:
            # treat as reuse and kill the whole family.
            _revoke_family(session, row.family_id)
            session.commit()
            return "reuse", None, None
        # Valid: revoke this row, issue its successor in the same family.
        row.revoked_at = now
        new_raw = secrets.token_urlsafe(32)
        new_row = RefreshTokenRow(
            id=str(uuid4()),
            user_id=row.user_id,
            token_hash=_hash_token(new_raw),
            family_id=row.family_id,
            expires_at=now + REFRESH_TOKEN_TTL,
            revoked_at=None,
        )
        session.add(new_row)
        session.commit()
        session.refresh(new_row)
        return "ok", new_raw, new_row
    finally:
        session.close()


def revoke_refresh_token(raw: str) -> bool:
    """Revoke one presented refresh token (logout). Returns True when a
    row was found and revoked; False for an unknown/stale token (the
    caller still clears the cookie and returns 200)."""
    session = db.get_session()
    try:
        row = (
            session.query(RefreshTokenRow)
            .filter(RefreshTokenRow.token_hash == _hash_token(raw))
            .first()
        )
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = datetime.utcnow()
        session.commit()
        return True
    finally:
        session.close()


def revoke_all_refresh_tokens(user_id: str) -> int:
    """Revoke every refresh token of a user (all sessions, all devices).
    Used by change-password. Returns the number revoked."""
    session = db.get_session()
    try:
        count = (
            session.query(RefreshTokenRow)
            .filter(
                RefreshTokenRow.user_id == user_id,
                RefreshTokenRow.revoked_at.is_(None),
            )
            .update({"revoked_at": datetime.utcnow()})
        )
        session.commit()
        return count
    finally:
        session.close()


def user_from_access_token(token: str) -> UserRow | None:
    """Validate an access token and load its user. None when invalid."""
    payload = verify_access_token(token)
    if payload is None:
        return None
    return get_user(payload["sub"])


def public_user_dict(user: UserRow) -> dict:
    """The only user shape the API ever returns. Never includes the
    password hash."""
    return {
        "id": user.id,
        "email": user.email,
        "onboarding_completed": bool(user.onboarding_completed),
    }


# Re-exported for tests / callers that need the env default limit.
def default_auto_approve_limit_inr() -> int:
    return settings.auto_approve_limit_inr
