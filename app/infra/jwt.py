"""HS256 JWT helpers for Elric access tokens (v1.1).

Implemented on the standard library only (hmac / hashlib / base64) —
no PyJWT dependency. Access tokens are short-lived (15 minutes) and
carry {"sub": user_id, "email": ..., "type": "access"}. Refresh tokens
are opaque random strings handled by app/services/auth_service.py, not
JWTs, so nothing here ever touches them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.config import settings

ACCESS_TOKEN_TTL_SECONDS = 15 * 60  # 15 minutes, per the pinned token contract


def require_jwt_secret() -> str:
    """The signing secret. Fails loudly when unset — the app must never
    start (or mint tokens) with a fallback/empty secret."""
    secret = settings.jwt_secret
    if not secret:
        raise RuntimeError(
            "JWT_SECRET is not configured — set it in .env or the "
            "environment; Elric refuses to start without one"
        )
    return secret


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def encode_access_token(user_id: str, email: str) -> str:
    """Mint a 15-minute access JWT for the user."""
    secret = require_jwt_secret()
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": user_id,
                "email": email,
                "type": "access",
                "iat": now,
                "exp": now + ACCESS_TOKEN_TTL_SECONDS,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(
        hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


def decode_access_token(token: str) -> dict | None:
    """Verify an access JWT. Returns the payload, or None when the token
    is malformed, has a bad signature, is expired, or is not an access
    token. Never raises for a bad token — callers map None to 401."""
    secret = settings.jwt_secret
    if not secret:
        return None
    try:
        header_b64, payload_b64, signature = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode()
        expected = _b64url(
            hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected, signature):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if payload.get("type") != "access":
            return None
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        if not payload.get("sub"):
            return None
        return payload
    except Exception:  # noqa: BLE001 — any malformed token is just invalid
        return None
