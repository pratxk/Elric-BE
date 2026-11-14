"""Gemini client — thin HTTP wrapper over the Gemini REST API.

Structured output via responseSchema. Primary key first, secondary on 429
(same fallback pattern as Almighty). No call is ever made without a key:
missing keys raise a clear ConfigurationError. This module is never called
during the scaffold — extraction is stubbed at the service layer.
"""

from __future__ import annotations

import httpx

from app.config import settings

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


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

        Tries the primary key, falls back to the secondary on 429.
        Returns the parsed JSON object. Raises on non-429 errors.
        When document bytes are given they are sent as an inlineData part
        (Gemini vision for scanned/image docs; text layer is in the prompt).
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
                    json={
                        "contents": [{"parts": parts}],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "responseSchema": response_schema,
                        },
                    },
                    timeout=timeout,
                )
                if resp.status_code == 429:
                    last_error = RuntimeError(f"Gemini 429 for key ...{key[-4:]}")
                    continue
                resp.raise_for_status()
                data = resp.json()
                import json as _json

                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return _json.loads(text)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    last_error = exc
                    continue
                raise
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
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json"},
                    },
                    timeout=timeout,
                )
                if resp.status_code == 429:
                    last_error = RuntimeError(f"Gemini 429 for key ...{key[-4:]}")
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    last_error = exc
                    continue
                raise
        raise RuntimeError(f"Gemini request failed after key fallback: {last_error}")
