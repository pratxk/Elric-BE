"""Gemini client rollover tests — fake HTTP transport, zero real calls.

Covers the primary → secondary fallback on quota (429) and on a dead /
invalid key (400 + "API key not valid"), and that other errors do not
roll over. Key material never appears in errors or logs.
"""

import json

import httpx
import pytest

from app.infra.gemini_client import GeminiClient, GeminiTransientError, _key_failed


def _resp(status, payload, headers=None):
    req = httpx.Request("POST", "https://example.invalid/")
    return httpx.Response(status, json=payload, request=req, headers=headers or {})


def _ok_payload():
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps({"ok": True})}]}}
        ]
    }


def _invalid_key_payload():
    return {
        "error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT",
        }
    }


def test_key_failed_predicate():
    assert _key_failed(429, "")
    assert _key_failed(400, json.dumps(_invalid_key_payload()))
    assert _key_failed(401, json.dumps(_invalid_key_payload()))
    assert not _key_failed(400, '{"error": {"message": "Invalid JSON payload"}}')
    assert not _key_failed(404, "")
    assert not _key_failed(500, "")


def test_invalid_primary_key_rolls_to_secondary(monkeypatch):
    calls = []

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        calls.append(params.get("key"))
        if params.get("key") == "primary-key":
            return _resp(400, _invalid_key_payload())
        return _resp(200, _ok_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(
        api_key="primary-key",
        secondary_api_key="secondary-key",
        model="gemini-2.5-flash",
    )
    result = client.generate_structured(
        "ping", {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    )
    assert result == {"ok": True}
    assert calls == ["primary-key", "secondary-key"]


def test_quota_429_retried_then_rolls_to_secondary(monkeypatch):
    """429 is retried with backoff on the primary first (3 attempts,
    1s + 2s), and only then rolls to the secondary — a per-minute rate
    limit recovers without ever touching the second key."""
    calls = []
    sleeps = []

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        calls.append(params.get("key"))
        if params.get("key") == "primary-key":
            return _resp(429, {"error": {"message": "Quota exceeded"}})
        return _resp(200, _ok_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(
        api_key="primary-key",
        secondary_api_key="secondary-key",
        model="gemini-2.5-flash",
        sleep=sleeps.append,
    )
    result = client.generate_structured("ping", {"type": "object"})
    assert result == {"ok": True}
    assert calls == ["primary-key", "primary-key", "primary-key", "secondary-key"]
    assert sleeps == [1.0, 2.0]


def test_invalid_primary_key_rolls_to_secondary_for_text(monkeypatch):
    calls = []

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        calls.append(params.get("key"))
        if params.get("key") == "primary-key":
            return _resp(400, _invalid_key_payload())
        return _resp(
            200,
            {"candidates": [{"content": {"parts": [{"text": '{"ok": true}' }]}}]},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(
        api_key="primary-key",
        secondary_api_key="secondary-key",
        model="gemini-2.5-flash",
    )
    assert json.loads(client.generate_text("ping")) == {"ok": True}
    assert calls == ["primary-key", "secondary-key"]


def test_both_keys_dead_raises_without_leaking_key_material(monkeypatch):
    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        return _resp(400, _invalid_key_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(
        api_key="primary-key",
        secondary_api_key="secondary-key",
        model="gemini-2.5-flash",
    )
    with pytest.raises(RuntimeError, match="failed after key fallback") as excinfo:
        client.generate_structured("ping", {"type": "object"})
    assert "primary-key" not in str(excinfo.value)
    assert "secondary-key" not in str(excinfo.value)


def test_failed_request_error_never_contains_key_material(monkeypatch):
    """Regression: httpx embeds ?key= in HTTPStatusError's message, so
    a raw propagation would leak the key into logs and tracebacks."""
    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        return _resp(400, {"error": {"message": "Invalid JSON payload"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(
        api_key="primary-key",
        secondary_api_key="secondary-key",
        model="gemini-2.5-flash",
    )
    with pytest.raises(RuntimeError) as excinfo:
        client.generate_structured("ping", {"type": "object"})
    assert "primary-key" not in str(excinfo.value)
    assert "secondary-key" not in str(excinfo.value)
    assert "key=" not in str(excinfo.value)


def test_malformed_request_does_not_roll_over(monkeypatch):
    """A 400 that is NOT a key problem must fail fast on the primary —
    retrying it on the secondary would burn quota on a request that
    fails identically."""
    calls = []

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        calls.append(params.get("key"))
        return _resp(400, {"error": {"message": "Invalid JSON payload"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(
        api_key="primary-key",
        secondary_api_key="secondary-key",
        model="gemini-2.5-flash",
    )
    with pytest.raises(RuntimeError, match="Gemini request failed: HTTP 400"):
        client.generate_structured("ping", {"type": "object"})
    assert calls == ["primary-key"]


def test_client_passes_explicit_proxy_from_env(monkeypatch):
    """Regression: httpx 0.28.1 raises InvalidURL on the unbracketed
    IPv6 entries in this VM's NO_PROXY when it resolves the proxy from
    the environment, which broke every Gemini call even though the
    proxy itself works. The client must pass the proxy explicitly."""
    seen = {}

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        seen.update(kwargs)
        return _resp(200, _ok_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    client = GeminiClient(api_key="k", secondary_api_key=None, model="m")
    client.generate_structured("ping", {"type": "object"})
    assert seen.get("proxy") == "http://proxy.example:3128"


def test_client_passes_no_proxy_when_env_unset(monkeypatch):
    seen = {}

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        seen.update(kwargs)
        return _resp(200, _ok_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    client = GeminiClient(api_key="k", secondary_api_key=None, model="m")
    client.generate_structured("ping", {"type": "object"})
    assert seen.get("proxy") is None

# --- retry-with-backoff (v1.1 fix round) ---------------------------
# A transient 503/429 is retried (3 attempts, exponential backoff)
# before the call gives up; the give-up error is a typed
# GeminiTransientError carrying the status. Fake transport, zero
# real calls; sleeps are recorded, never slept.


def _overloaded_payload():
    return {
        "error": {
            "code": 503,
            "message": "The model is overloaded. Please try again later.",
            "status": "UNAVAILABLE",
        }
    }


def test_503_retried_twice_then_succeeds(monkeypatch):
    """The verifier's case: gemini-3.8-flash 503s, then recovers. The
    call must succeed after 2 backoff sleeps (1s, 2s) — the invoice is
    never parked on a transient 503."""
    calls = []
    sleeps = []
    outcomes = iter([503, 503, 200])

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        calls.append(next(outcomes))
        status = calls[-1]
        if status == 200:
            return _resp(200, _ok_payload())
        return _resp(503, _overloaded_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(api_key="k", secondary_api_key=None, model="m", sleep=sleeps.append)
    assert client.generate_structured("ping", {"type": "object"}) == {"ok": True}
    assert calls == [503, 503, 200]
    assert sleeps == [1.0, 2.0]


def test_503_retry_honours_retry_after_header(monkeypatch):
    sleeps = []
    seen = {"n": 0}

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return _resp(503, _overloaded_payload(), headers={"retry-after": "5"})
        return _resp(200, _ok_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(api_key="k", secondary_api_key=None, model="m", sleep=sleeps.append)
    assert client.generate_structured("ping", {"type": "object"}) == {"ok": True}
    assert sleeps == [5.0]


def test_503_exhausted_on_all_keys_raises_typed_transient_error(monkeypatch):
    """Both keys 503 three times each: 6 attempts, then a typed
    GeminiTransientError(503) — the message says 'overloaded', never
    anything the UI could render as 'no key configured'."""
    calls = []
    sleeps = []

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        calls.append(params.get("key"))
        return _resp(503, _overloaded_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(
        api_key="primary-key",
        secondary_api_key="secondary-key",
        model="m",
        sleep=sleeps.append,
    )
    with pytest.raises(GeminiTransientError) as excinfo:
        client.generate_structured("ping", {"type": "object"})
    assert excinfo.value.status == 503
    assert "overloaded" in str(excinfo.value).lower()
    assert "no key" not in str(excinfo.value).lower()
    assert calls == ["primary-key"] * 3 + ["secondary-key"] * 3
    assert sleeps == [1.0, 2.0, 1.0, 2.0]
    assert "primary-key" not in str(excinfo.value)
    assert "secondary-key" not in str(excinfo.value)


def test_429_exhausted_raises_typed_transient_error(monkeypatch):
    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        return _resp(429, {"error": {"message": "Quota exceeded"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(api_key="k", secondary_api_key=None, model="m", sleep=lambda s: None)
    with pytest.raises(GeminiTransientError) as excinfo:
        client.generate_text("ping")
    assert excinfo.value.status == 429
    assert "quota" in str(excinfo.value).lower()


def test_503_retry_applies_to_generate_text_too(monkeypatch):
    seen = {"n": 0}

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return _resp(503, _overloaded_payload())
        return _resp(
            200,
            {"candidates": [{"content": {"parts": [{"text": '{"ok": true}' }]}}]},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(api_key="k", secondary_api_key=None, model="m", sleep=lambda s: None)
    assert json.loads(client.generate_text("ping")) == {"ok": True}
    assert seen["n"] == 2


def test_non_transient_400_still_fails_fast_without_retry(monkeypatch):
    """A malformed-request 400 is not transient: exactly one attempt,
    no sleeps, no rollover."""
    calls = []
    sleeps = []

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        calls.append(params.get("key"))
        return _resp(400, {"error": {"message": "Invalid JSON payload"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = GeminiClient(
        api_key="primary-key",
        secondary_api_key="secondary-key",
        model="m",
        sleep=sleeps.append,
    )
    with pytest.raises(RuntimeError, match="Gemini request failed: HTTP 400"):
        client.generate_structured("ping", {"type": "object"})
    assert calls == ["primary-key"]
    assert sleeps == []
