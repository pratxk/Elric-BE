"""Gemini client — thin HTTP wrapper over the Gemini REST API.

Structured output via responseSchema. Primary key first, secondary on
quota exhaustion (429) or a dead/invalid key (same fallback pattern as
Almighty, extended to key errors so a revoked key rolls over too). No
call is ever made without a key: missing keys raise a clear
ConfigurationError. Key material never appears in errors or logs.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Retry policy for transient Gemini failures (503 overload, 429 quota):
# at most this many attempts per key, with exponential backoff between
# them (base * 2**n, plus a Retry-After header when the API sends one).
# A single transient 503 must never silently park an invoice — the
# extractor only gives up after every key has exhausted its attempts.
MAX_ATTEMPTS_PER_KEY = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0


def _proxy_url() -> str | None:
    """Explicit egress proxy for httpx, or None when there is none.

    httpx 0.28.1 raises InvalidURL on the unbracketed IPv6 entries in
    this VM's NO_PROXY when it resolves the proxy from the environment,
    which breaks every Gemini call even though the proxy itself works
    (curl reaches Google fine through it). Passing the proxy explicitly
    skips that resolution while keeping env-based TLS config. On Render
    (or anywhere without a proxy env var) this returns None and httpx
    behaves exactly as before.
    """
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def _key_failed(status: int, body: str) -> bool:
    """True when a key failure means 'try the next key'.

    Quota exhaustion (429) rolls over, and so does a dead key: Gemini
    answers an invalid key with 400 + "API key not valid" (status
    INVALID_ARGUMENT). Other 4xx (malformed request, bad model, …) do
    NOT roll over — retrying them on the second key would burn quota
    on a request that fails identically.
    """
    if status == 429:
        return True
    if status in (400, 401, 403):
        lowered = body.lower()
        return "api_key_invalid" in lowered or "api key not valid" in lowered
    return False


class GeminiConfigurationError(RuntimeError):
    """No Gemini API key is configured."""


class GeminiTransientError(RuntimeError):
    """A retryable Gemini failure that survived every attempt (503 model
    overload or 429 quota exhaustion). Carries the HTTP status so the
    caller can tell the user the TRUE reason instead of guessing."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Retry-After header (seconds) when present and parseable."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _is_transient(status: int) -> bool:
    """Retryable statuses: 503 model overload, 429 quota exhaustion."""
    return status in (429, 503)


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        secondary_api_key: str | None = None,
        model: str | None = None,
        max_attempts_per_key: int = MAX_ATTEMPTS_PER_KEY,
        backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
        sleep=time.sleep,
    ):
        self.primary_key = api_key or settings.gemini_api_key
        self.secondary_key = secondary_api_key or settings.gemini_api_key_secondary
        self.model = model or settings.gemini_model
        self.max_attempts_per_key = max_attempts_per_key
        self.backoff_base_seconds = backoff_base_seconds
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.primary_key)

    def _keys(self) -> list[str]:
        keys = [k for k in (self.primary_key, self.secondary_key) if k]
        if not keys:
            raise GeminiConfigurationError(
                "no Gemini API key configured — set GEMINI_API_KEY "
                "(and optionally GEMINI_API_KEY_SECONDARY) in the environment"
            )
        return keys

    def _post_with_retries(self, key: str, payload: dict, timeout: float) -> httpx.Response:
        """POST generateContent with retry-with-backoff on transient
        statuses (503 overload, 429 quota). Returns the first
        non-transient response, or raises GeminiTransientError after
        the attempts are exhausted. Transport failures (DNS, timeout)
        are NOT retried here — the request never reached Gemini, so
        retrying cannot help and the error is raised as-is.
        """
        last_resp: httpx.Response | None = None
        for attempt in range(1, self.max_attempts_per_key + 1):
            resp = httpx.post(
                f"{GEMINI_BASE}/models/{self.model}:generateContent",
                params={"key": key},
                proxy=_proxy_url(),
                json=payload,
                timeout=timeout,
            )
            if not _is_transient(resp.status_code):
                return resp
            last_resp = resp
            if attempt < self.max_attempts_per_key:
                retry_after = _retry_after_seconds(resp)
                delay = (
                    min(retry_after, MAX_BACKOFF_SECONDS)
                    if retry_after is not None
                    else min(
                        self.backoff_base_seconds * (2 ** (attempt - 1)),
                        MAX_BACKOFF_SECONDS,
                    )
                )
                logger.warning(
                    "Gemini HTTP %s (attempt %s/%s); retrying in %.1fs",
                    resp.status_code,
                    attempt,
                    self.max_attempts_per_key,
                    delay,
                )
                self._sleep(delay)
        assert last_resp is not None
        status = last_resp.status_code
        if status == 503:
            raise GeminiTransientError(
                status,
                "Gemini overloaded (HTTP 503) — the model is temporarily "
                "unavailable; try again in a few minutes",
            )
        raise GeminiTransientError(
            status,
            "Gemini quota exhausted (HTTP 429) — the API key's quota is "
            "used up; try again later",
        )

    def generate_structured(
        self,
        prompt: str,
        response_schema: dict,
        timeout: float = 120.0,
        document_bytes: bytes | None = None,
        mime_type: str | None = None,
    ) -> dict:
        """Call generateContent with JSON-schema structured output.

        Tries the primary key, falls back to the secondary on quota
        exhaustion (429) or a dead/invalid key. Each key gets up to
        MAX_ATTEMPTS_PER_KEY attempts with exponential backoff on
        transient statuses (503 overload, 429 quota) before the client
        moves on — a single transient 503 never fails the call. Returns
        the parsed JSON object. Raises GeminiTransientError when every
        key exhausts its attempts on a transient status. When document
        bytes are given they are sent as an inlineData part (Gemini
        vision for scanned/image docs; text layer is in the prompt).
        """
        parts: list[dict] = [{"text": prompt}]
        if document_bytes:
            import base64

            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime_type or "application/pdf",
                        "data": base64.b64encode(document_bytes).decode("ascii"),
                    }
                }
            )
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": response_schema,
            },
        }
        last_error: Exception | None = None
        for key in self._keys():
            try:
                resp = self._post_with_retries(key, payload, timeout)
            except httpx.HTTPError as exc:
                # Transport failure (DNS, timeout, …): do not burn the
                # other key on a request that never reached Gemini.
                raise exc
            except GeminiTransientError as exc:
                # Transient even after retries on this key: try the next
                # key (overload / quota can be project-scoped). Only
                # give up once every key is exhausted.
                last_error = exc
                continue
            if _key_failed(resp.status_code, resp.text):
                last_error = RuntimeError(
                    "Gemini key rejected or quota exhausted "
                    f"(HTTP {resp.status_code})"
                )
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError:
                # Log the API's error body (it never contains key
                # material) so a 400 is diagnosable from server logs
                # without spending another call to reproduce it. Then
                # raise a sanitized error: httpx embeds the full
                # request URL — including ?key= — in HTTPStatusError's
                # message, which would leak the key into logs and
                # tracebacks. Never propagate it.
                logger.warning(
                    "Gemini generateContent HTTP %s: %s",
                    resp.status_code,
                    resp.text[:500],
                )
                raise RuntimeError(
                    f"Gemini request failed: HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                ) from None
            data = resp.json()
            import json as _json

            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return _json.loads(text)
        if isinstance(last_error, GeminiTransientError):
            raise last_error
        raise RuntimeError(f"Gemini request failed after key fallback: {last_error}")

    def generate_text(self, prompt: str, timeout: float = 120.0) -> str:
        """Plain-text generation with the same primary → secondary fallback
        and the same retry-with-backoff on transient statuses.

        Used for root-cause analysis (§16b.1), where the output is JSON
        parsed by the caller. No responseSchema: the prompt asks for JSON.
        """
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        last_error: Exception | None = None
        for key in self._keys():
            try:
                resp = self._post_with_retries(key, payload, timeout)
            except httpx.HTTPError as exc:
                # Transport failure: do not burn the other key on a
                # request that never reached Gemini.
                raise exc
            except GeminiTransientError as exc:
                last_error = exc
                continue
            if _key_failed(resp.status_code, resp.text):
                last_error = RuntimeError(
                    "Gemini key rejected or quota exhausted "
                    f"(HTTP {resp.status_code})"
                )
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError:
                logger.warning(
                    "Gemini generateContent HTTP %s: %s",
                    resp.status_code,
                    resp.text[:500],
                )
                raise RuntimeError(
                    f"Gemini request failed: HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                ) from None
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        if isinstance(last_error, GeminiTransientError):
            raise last_error
        raise RuntimeError(f"Gemini request failed after key fallback: {last_error}")
