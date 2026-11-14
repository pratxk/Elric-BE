"""Shared helpers for API tests (v1.1 auth).

Every /api/* endpoint except /health and the public auth endpoints
requires a Bearer access token, so tests build an authenticated
TestClient: register a fresh user, then send the access token as a
default header on every request. Each call returns a NEW client (fresh
cookie jar) so refresh-token tests never leak cookies across tests.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

STRONG_PASSWORD = "Str0ng!Passw0rd"
OTHER_STRONG_PASSWORD = "An0ther!Passw0rd"


def make_client(email: str = "test@elric.dev", password: str = STRONG_PASSWORD) -> TestClient:
    """A TestClient authenticated as a freshly registered user. The
    access token is a default header; the elric_refresh cookie sits in
    the client's jar (set by /register)."""
    client = TestClient(app)
    resp = client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    token = body["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    # Stash the user id for tests that seed rows directly in the store.
    client.user_id = body["user"]["id"]
    return client


def login_client(email: str = "test@elric.dev", password: str = STRONG_PASSWORD) -> TestClient:
    """A TestClient logged in as an EXISTING user (second session /
    second device for the same account)."""
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    client.headers["Authorization"] = f"Bearer {body['access_token']}"
    client.user_id = body["user"]["id"]
    return client


def make_plain_client() -> TestClient:
    """An unauthenticated TestClient (no token, empty cookie jar)."""
    return TestClient(app)
