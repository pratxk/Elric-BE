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

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


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


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        secondary_api_key: str | None = None,
        model: str | None = None,
    ):
        self.primary_key = api_key or settings.gemini_api_key
        self.secondary_key = secondary_api_key or settings.gemini_api_key_secondary
        self.model = model or settings.gemini_model

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
        exhaustion (429) or a dead/invalid key. Returns the parsed JSON
        object. Raises on other errors. When document bytes are given
        they are sent as an inlineData part (Gemini vision for
        scanned/image docs; text layer is in the prompt).
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
        last_error: Exception | None = None
        for key in self._keys():
            try:
                resp = httpx.post(
                    f"{GEMINI_BASE}/models/{self.model}:generateContent",
                    params={"key": key},
                    proxy=_proxy_url(),
                    json={
                        "contents": [{"parts": parts}],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "responseSchema": response_schema,
                        },
                    },
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                # Transport failure (DNS, timeout, …): do not burn the
                # other key on a request that never reached Gemini.
                raise exc
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
        raise RuntimeError(f"Gemini request failed after key fallback: {last_error}")

    def generate_text(self, prompt: str, timeout: float = 120.0) -> str:
        """Plain-text generation with the same primary → secondary fallback.

        Used for root-cause analysis (§16b.1), where the output is JSON
        parsed by the caller. No responseSchema: the prompt asks for JSON.
        """
        last_error: Exception | None = None
        for key in self._keys():
            try:
                resp = httpx.post(
                    f"{GEMINI_BASE}/models/{self.model}:generateContent",
                    params={"key": key},
                    proxy=_proxy_url(),
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json"},
                    },
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                # Transport failure: do not burn the other key on a
                # request that never reached Gemini.
                raise exc
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
        raise RuntimeError(f"Gemini request failed after key fallback: {last_error}")
