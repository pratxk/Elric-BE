"""Auth tests (v1.1) — email + password, access + refresh tokens.

Token contract under test (pinned): access JWT 15 min (Bearer),
opaque refresh token 7 days in the HttpOnly `elric_refresh` cookie,
rotation in the same family, reuse detection revoking the whole
family, logout revocation, change-password revoking all sessions.

ZERO real Gemini calls here (and everywhere in the suite).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time

import pytest

from app.config import settings
from tests.helpers import (
    OTHER_STRONG_PASSWORD,
    STRONG_PASSWORD,
    login_client,
    make_client,
    make_plain_client,
)

GENERIC_ERROR = "Invalid email or password"


# -- helpers ---------------------------------------------------------------

def _expired_access_token(user_id: str, email: str) -> str:
    """A correctly signed access token whose exp is already past —
    built with the test JWT secret, without touching app internals."""
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(
        json.dumps(
            {
                "sub": user_id,
                "email": email,
                "type": "access",
                "iat": now - 1200,
                "exp": now - 300,
            }
        ).encode()
    )
    sig = b64(
        hmac.new(
            settings.jwt_secret.encode(), f"{header}.{payload}".encode(),
            hashlib.sha256,
        ).digest()
    )
    return f"{header}.{payload}.{sig}"


def _refresh_cookie(client) -> str | None:
    return client.cookies.get("elric_refresh")


# -- register ---------------------------------------------------------------

def test_register_valid_returns_session():
    client = make_plain_client()
    resp = client.post(
        "/api/auth/register",
        json={"email": "Alice@Example.com", "password": STRONG_PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user"]["email"] == "alice@example.com"  # normalized
    assert body["user"]["onboarding_completed"] is False
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    # Refresh cookie: HttpOnly, SameSite=Lax, Path=/, NOT Secure on HTTP.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "elric_refresh=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()
    # No hash anywhere in the response.
    assert "password_hash" not in resp.text
    assert STRONG_PASSWORD not in resp.text
    # The session works.
    me = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


def test_register_hash_is_bcrypt_not_plaintext():
    client = make_plain_client()
    client.post(
        "/api/auth/register",
        json={"email": "hash@elric.dev", "password": STRONG_PASSWORD},
    )
    from app.services.auth_service import get_user_by_email

    row = get_user_by_email("hash@elric.dev")
    assert row is not None
    assert row.password_hash != STRONG_PASSWORD
    assert row.password_hash.startswith("$2b$12$")  # bcrypt, cost 12
    assert STRONG_PASSWORD not in row.password_hash


@pytest.mark.parametrize(
    "password",
    [
        "short1!",  # too short
        "alllowercase1!",  # no uppercase
        "ALLUPPERCASE1!",  # no lowercase
        "NoDigitsHere!",  # no digit
        "NoSymbol1234",  # no symbol
        "",  # empty
    ],
)
def test_register_weak_password_422(password):
    client = make_plain_client()
    resp = client.post(
        "/api/auth/register",
        json={"email": "weak@elric.dev", "password": password},
    )
    assert resp.status_code == 422, resp.text
    assert "password" in resp.text.lower()


@pytest.mark.parametrize("email", ["not-an-email", "missing-at.com", "@nope.com", ""])
def test_register_bad_email_422(email):
    client = make_plain_client()
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": STRONG_PASSWORD},
    )
    assert resp.status_code == 422, resp.text


def test_register_duplicate_409():
    client = make_plain_client()
    body = {"email": "dup@elric.dev", "password": STRONG_PASSWORD}
    assert client.post("/api/auth/register", json=body).status_code == 201
    resp = client.post("/api/auth/register", json=body)
    assert resp.status_code == 409
    assert "already exists" in resp.text


# -- login / lockout ---------------------------------------------------------

def test_login_valid_returns_session():
    client = make_plain_client()
    client.post(
        "/api/auth/register",
        json={"email": "login@elric.dev", "password": STRONG_PASSWORD},
    )
    resp = client.post(
        "/api/auth/login",
        json={"email": "login@elric.dev", "password": STRONG_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]
    assert "elric_refresh=" in resp.headers.get("set-cookie", "")
    # Access token really is a 15-minute JWT.
    from app.infra.jwt import decode_access_token

    payload = decode_access_token(resp.json()["access_token"])
    assert payload is not None
    assert payload["exp"] - payload["iat"] == 900


def test_login_wrong_password_generic_401():
    client = make_plain_client()
    client.post(
        "/api/auth/register",
        json={"email": "wp@elric.dev", "password": STRONG_PASSWORD},
    )
    resp = client.post(
        "/api/auth/login",
        json={"email": "wp@elric.dev", "password": "Wr0ng!Passw0rd"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == GENERIC_ERROR


def test_login_unknown_email_identical_401():
    client = make_plain_client()
    resp = client.post(
        "/api/auth/login",
        json={"email": "nobody@elric.dev", "password": "Wr0ng!Passw0rd"},
    )
    assert resp.status_code == 401
    # Identical message to the wrong-password case: no enumeration.
    assert resp.json()["detail"] == GENERIC_ERROR


def test_login_lockout_after_five_failures():
    client = make_plain_client()
    email = "lock@elric.dev"
    client.post(
        "/api/auth/register", json={"email": email, "password": STRONG_PASSWORD}
    )
    for i in range(5):
        resp = client.post(
            "/api/auth/login",
            json={"email": email, "password": "Wr0ng!Passw0rd"},
        )
        assert resp.status_code == 401, f"attempt {i + 1}: {resp.text}"
    # 6th attempt — even with the CORRECT password — is locked out.
    resp = client.post(
        "/api/auth/login", json={"email": email, "password": STRONG_PASSWORD}
    )
    assert resp.status_code == 429
    assert "15 minutes" in resp.json()["detail"]
    # Failures are DB-backed rows (survive restart — the table is the store).
    from app.infra import db as db_mod
    from app.infra.db import LoginAttemptRow

    session = db_mod.get_session()
    try:
        count = (
            session.query(LoginAttemptRow)
            .filter(LoginAttemptRow.email == email)
            .count()
        )
    finally:
        session.close()
    assert count == 5


def test_login_success_clears_failed_attempts():
    client = make_plain_client()
    email = "clear@elric.dev"
    client.post(
        "/api/auth/register", json={"email": email, "password": STRONG_PASSWORD}
    )
    for _ in range(4):
        client.post(
            "/api/auth/login",
            json={"email": email, "password": "Wr0ng!Passw0rd"},
        )
    resp = client.post(
        "/api/auth/login", json={"email": email, "password": STRONG_PASSWORD}
    )
    assert resp.status_code == 200
    # A later failure starts the count from zero again.
    assert (
        client.post(
            "/api/auth/login",
            json={"email": email, "password": "Wr0ng!Passw0rd"},
        ).status_code
        == 401
    )


# -- access token / me / onboarding ------------------------------------------

def test_me_requires_auth():
    client = make_plain_client()
    assert client.get("/api/auth/me").status_code == 401


def test_me_rejects_garbage_and_expired_tokens():
    client = make_client()
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    user_id = me.json()["id"]
    plain = make_plain_client()
    assert (
        plain.get(
            "/api/auth/me", headers={"Authorization": "Bearer garbage.token.here"}
        ).status_code
        == 401
    )
    expired = _expired_access_token(user_id, "test@elric.dev")
    assert (
        plain.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {expired}"}
        ).status_code
        == 401
    )


def test_complete_onboarding():
    client = make_client()
    assert client.get("/api/auth/me").json()["onboarding_completed"] is False
    resp = client.post("/api/auth/complete-onboarding")
    assert resp.status_code == 200
    assert resp.json()["onboarding_completed"] is True
    assert client.get("/api/auth/me").json()["onboarding_completed"] is True


# -- refresh rotation + reuse detection --------------------------------------

def test_refresh_rotation_old_token_fails_new_works():
    client = make_client()
    old_cookie = _refresh_cookie(client)
    assert old_cookie
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]
    new_cookie = _refresh_cookie(client)
    assert new_cookie and new_cookie != old_cookie
    # The new access token works.
    assert (
        client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
        ).status_code
        == 200
    )


def test_refresh_reuse_detection_revokes_family():
    client = make_client()
    first_cookie = _refresh_cookie(client)
    # Rotate once: first -> second.
    assert client.post("/api/auth/refresh").status_code == 200
    second_cookie = _refresh_cookie(client)
    assert second_cookie != first_cookie
    # Replay the ALREADY-ROTATED first token: reuse → 401, and the
    # whole family dies — the second token is invalidated too.
    replay = make_plain_client()
    replay.cookies.set("elric_refresh", first_cookie)
    assert replay.post("/api/auth/refresh").status_code == 401
    victim = make_plain_client()
    victim.cookies.set("elric_refresh", second_cookie)
    assert victim.post("/api/auth/refresh").status_code == 401
    # Forced re-login: password login still works (fresh family).
    assert (
        victim.post(
            "/api/auth/login",
            json={"email": "test@elric.dev", "password": STRONG_PASSWORD},
        ).status_code
        == 200
    )


def test_refresh_unknown_token_401_without_nuking():
    client = make_client()
    good_cookie = _refresh_cookie(client)
    stranger = make_plain_client()
    stranger.cookies.set("elric_refresh", "definitely-not-a-real-token")
    assert stranger.post("/api/auth/refresh").status_code == 401
    # The user's real session is untouched by the unknown token.
    assert client.post("/api/auth/refresh").status_code == 200
    assert _refresh_cookie(client) != good_cookie


def test_refresh_without_cookie_401():
    assert make_plain_client().post("/api/auth/refresh").status_code == 401


def test_logout_revokes_refresh_token():
    client = make_client()
    cookie = _refresh_cookie(client)
    assert client.post("/api/auth/logout").status_code == 200
    assert _refresh_cookie(client) is None
    # The revoked token no longer refreshes.
    ghost = make_plain_client()
    ghost.cookies.set("elric_refresh", cookie)
    assert ghost.post("/api/auth/refresh").status_code == 401


# -- change password ----------------------------------------------------------

def test_change_password_wrong_current_401():
    client = make_client()
    resp = client.post(
        "/api/auth/change-password",
        json={
            "current_password": "Wr0ng!Passw0rd",
            "new_password": OTHER_STRONG_PASSWORD,
        },
    )
    assert resp.status_code == 401


def test_change_password_weak_new_422():
    client = make_client()
    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": STRONG_PASSWORD, "new_password": "weak"},
    )
    assert resp.status_code == 422


def test_change_password_revokes_all_sessions():
    client_a = make_client()
    client_b = login_client(email="test@elric.dev", password=STRONG_PASSWORD)
    # Two independent sessions (two refresh tokens, two families).
    assert _refresh_cookie(client_a) != _refresh_cookie(client_b)
    resp = client_a.post(
        "/api/auth/change-password",
        json={
            "current_password": STRONG_PASSWORD,
            "new_password": OTHER_STRONG_PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reauthenticate"] is True
    # Both sessions' refresh tokens are dead.
    assert client_a.post("/api/auth/refresh").status_code == 401
    assert client_b.post("/api/auth/refresh").status_code == 401
    # New password works; old password does not.
    plain = make_plain_client()
    assert (
        plain.post(
            "/api/auth/login",
            json={"email": "test@elric.dev", "password": OTHER_STRONG_PASSWORD},
        ).status_code
        == 200
    )
    assert (
        plain.post(
            "/api/auth/login",
            json={"email": "test@elric.dev", "password": STRONG_PASSWORD},
        ).status_code
        == 401
    )


# -- 401 sweep -----------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/invoices"),
        ("POST", "/api/invoices/upload"),
        ("GET", "/api/invoices/does-not-exist"),
        ("POST", "/api/invoices/x/approve"),
        ("POST", "/api/invoices/x/reject"),
        ("POST", "/api/invoices/x/query-vendor"),
        ("GET", "/api/invoices/x/trace"),
        ("GET", "/api/invoices/x/ledger"),
        ("GET", "/api/invoices/x/payments"),
        ("POST", "/api/purchase-orders"),
        ("GET", "/api/purchase-orders"),
        ("POST", "/api/goods-receipts"),
        ("GET", "/api/goods-receipts"),
        ("GET", "/api/ledger"),
        ("GET", "/api/payments"),
        ("GET", "/api/queue/exceptions"),
        ("GET", "/api/aging"),
        ("GET", "/api/vendors"),
        ("GET", "/api/vendors/x"),
        ("POST", "/api/vendors"),
        ("PUT", "/api/vendors/x"),
        ("DELETE", "/api/vendors/x"),
        ("GET", "/api/metrics"),
        ("GET", "/api/forecast"),
        ("POST", "/api/forecast/discount"),
        ("POST", "/api/eval/run"),
        ("GET", "/api/eval/summary"),
        ("GET", "/api/settings"),
        ("PUT", "/api/settings"),
        ("GET", "/api/auth/me"),
        ("POST", "/api/auth/complete-onboarding"),
        ("POST", "/api/auth/change-password"),
    ],
)
def test_every_endpoint_401_without_session(method, path):
    client = make_plain_client()
    resp = client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} → {resp.status_code}"


def test_health_stays_public():
    assert make_plain_client().get("/health").status_code == 200


# -- per-user scoping ------------------------------------------------------------

def test_scoping_lists_and_direct_access():
    client_a = make_client(email="a@elric.dev")
    client_b = make_client(email="b@elric.dev")
    # A uploads an invoice and creates a vendor.
    files = {"file": ("inv.pdf", io.BytesIO(b"%PDF-scope-1"), "application/pdf")}
    inv_id = client_a.post("/api/invoices/upload", files=files).json()["id"]
    vendor = client_a.post(
        "/api/vendors", json={"name": "Acme Corp", "gstin": "29ABCDE1234F1Z5"}
    ).json()
    # B sees neither in lists…
    assert inv_id not in [r["id"] for r in client_b.get("/api/invoices").json()]
    assert client_b.get("/api/vendors").json() == []
    # …nor by direct id (404, not 403 — ids are not enumerable).
    assert client_b.get(f"/api/invoices/{inv_id}").status_code == 404
    assert client_b.get(f"/api/vendors/{vendor['id']}").status_code == 404
    assert (
        client_b.put(
            f"/api/vendors/{vendor['id']}", json={"name": "Hijacked"}
        ).status_code
        == 404
    )
    assert client_b.delete(f"/api/vendors/{vendor['id']}").status_code == 404
    # A still sees both.
    assert client_a.get(f"/api/invoices/{inv_id}").status_code == 200
    assert client_a.get(f"/api/vendors/{vendor['id']}").status_code == 200


def test_legacy_null_user_rows_invisible():
    from app.infra.store import store

    store.create_invoice(
        {
            "id": "inv-legacy",
            "vendor_id": "vendor-a",
            "vendor_name": "Vendor A",
            "invoice_number": "LEG-1",
            "invoice_date": "2026-01-01",
            "status": "received",
            "total": "1000",
        }
    )
    client = make_client()
    assert "inv-legacy" not in [r["id"] for r in client.get("/api/invoices").json()]
    assert client.get("/api/invoices/inv-legacy").status_code == 404


# -- settings ----------------------------------------------------------------------

def test_settings_defaults_created_on_first_read():
    client = make_client()
    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["country"] == "IN"
    assert body["currency"] == "INR"
    assert body["company_name"] is None
    assert body["state"] is None
    assert float(body["auto_approve_limit_inr"]) == 500000
    assert float(body["tolerance_pct"]) == 2.0
    assert float(body["tolerance_flat_inr"]) == 500
    assert body["duplicate_window_days"] == 90


def test_settings_put_persists_and_returns():
    client = make_client()
    payload = {
        "company_name": "Acme India Pvt Ltd",
        "country": "IN",
        "state": "Karnataka",
        "currency": "INR",
        "auto_approve_limit_inr": "100000",
        "tolerance_pct": "5",
        "tolerance_flat_inr": "1000",
        "duplicate_window_days": 30,
    }
    resp = client.put("/api/settings", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["company_name"] == "Acme India Pvt Ltd"
    assert body["state"] == "Karnataka"
    assert float(body["auto_approve_limit_inr"]) == 100000
    assert float(body["tolerance_pct"]) == 5
    assert body["duplicate_window_days"] == 30
    assert client.get("/api/settings").json() == body


@pytest.mark.parametrize(
    "overrides",
    [
        {"auto_approve_limit_inr": "-1"},
        {"tolerance_pct": "101"},
        {"tolerance_pct": "-1"},
        {"tolerance_flat_inr": "-5"},
        {"duplicate_window_days": 0},
        {"duplicate_window_days": 366},
    ],
)
def test_settings_invalid_422(overrides):
    client = make_client()
    base = {
        "company_name": None,
        "country": "IN",
        "state": None,
        "currency": "INR",
        "auto_approve_limit_inr": "100000",
        "tolerance_pct": "2",
        "tolerance_flat_inr": "500",
        "duplicate_window_days": 90,
    }
    base.update(overrides)
    assert client.put("/api/settings", json=base).status_code == 422


def _perfect_case_upload(client, settings_limit: str | None = None):
    """Seed a PO + upload a perfect invoice with mocked extraction.
    Returns the invoice id. Mirrors tests/test_api_v1.py."""
    from app.agents import extractor as extractor_mod
    from app.eval.synthetic import generate_corpus
    from app.services.extraction_service import ExtractionService
    from tests.test_extraction import FakeClient, _perfect_payload

    if settings_limit is not None:
        current = client.get("/api/settings").json()
        current["auto_approve_limit_inr"] = settings_limit
        assert client.put("/api/settings", json=current).status_code == 200
    case = next(
        c for c in generate_corpus(n_per_scenario=1) if c["scenario"] == "perfect"
    )
    po = case["po"]
    resp = client.post(
        "/api/purchase-orders",
        json={
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "vendor_name": "Acme Corporation Pvt Ltd",
            "po_date": str(po.po_date),
            "total": str(po.total),
            "lines": [
                {
                    "description": ln.description,
                    "hsn_code": ln.hsn_code,
                    "qty": str(ln.qty),
                    "unit_price": str(ln.unit_price),
                    "tax_rate_pct": str(ln.tax_rate_pct),
                }
                for ln in po.lines
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    payload = _perfect_payload()
    inv = case["invoice"]
    payload["invoice_number"] = inv.invoice_number
    payload["invoice_date"] = str(inv.invoice_date)
    payload["due_date"] = str(inv.due_date)
    payload["lines"] = [
        {
            "description": ln.description,
            "hsn_code": ln.hsn_code,
            "qty": float(ln.qty),
            "unit_price": float(ln.unit_price),
            "tax_rate_pct": float(ln.tax_rate_pct),
        }
        for ln in inv.lines
    ]
    payload["subtotal"] = float(inv.subtotal)
    payload["tax_total"] = float(inv.tax_total)
    payload["total"] = float(inv.total)
    old = extractor_mod._service
    extractor_mod._service = ExtractionService(client=FakeClient([payload]))
    try:
        files = {
            "file": (
                "inv.pdf",
                io.BytesIO(b"%PDF-settings-pipeline"),
                "application/pdf",
            )
        }
        resp = client.post("/api/invoices/upload", files=files)
        assert resp.status_code == 202, resp.text
        return resp.json()["id"]
    finally:
        extractor_mod._service = old


def test_settings_limit_zero_blocks_auto_approve():
    client = make_client()
    invoice_id = _perfect_case_upload(client, settings_limit="0")
    got = client.get(f"/api/invoices/{invoice_id}").json()
    # Clean match, but ₹0 limit → not auto-approved → waits for a human.
    assert got["match"]["outcome"] == "clean"
    assert got["status"] == "flagged"


def test_settings_limit_high_auto_approves():
    client = make_client()
    invoice_id = _perfect_case_upload(client, settings_limit="10000000")
    got = client.get(f"/api/invoices/{invoice_id}").json()
    assert got["match"]["outcome"] == "clean"
    assert got["status"] == "posted"


# -- vendor CRUD ---------------------------------------------------------------------

def test_vendors_crud():
    client = make_client()
    created = client.post(
        "/api/vendors",
        json={
            "name": "Acme Corp",
            "gstin": "29ABCDE1234F1Z5",
            "state": "Karnataka",
            "payment_terms": "2/10 Net 30",
            "email": "ap@acme.example",
        },
    )
    assert created.status_code == 201, created.text
    vendor_id = created.json()["id"]
    assert created.json()["name"] == "Acme Corp"
    got = client.get(f"/api/vendors/{vendor_id}")
    assert got.status_code == 200
    assert got.json()["payment_terms"] == "2/10 Net 30"
    updated = client.put(
        f"/api/vendors/{vendor_id}",
        json={"name": "Acme Corp Ltd", "payment_terms": "Net 30"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Acme Corp Ltd"
    assert updated.json()["payment_terms"] == "Net 30"
    assert client.delete(f"/api/vendors/{vendor_id}").status_code == 204
    assert client.get(f"/api/vendors/{vendor_id}").status_code == 404


def test_vendors_bad_email_422():
    client = make_client()
    resp = client.post(
        "/api/vendors", json={"name": "Bad Email Co", "email": "not-an-email"}
    )
    assert resp.status_code == 422
