# gateway/providers/gemini_client.py
# Google Gemini provider client.
# Endpoint: https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
# Auth: key= query param from GEMINI_API_KEY
# Maps OpenAI-style messages to Gemini's `contents` format internally.
# Spec: docs/02_ARCHITECTURE.md §6

from __future__ import annotations

import logging

import httpx

from gateway.config import get_settings
from gateway.providers.base import ProviderClient, ProviderError, ProviderResponse

logger = logging.getLogger(__name__)

_GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_TIMEOUT = 8.0  # seconds — per spec

# Gemini role mapping from OpenAI roles
_ROLE_MAP = {
    "user": "user",
    "assistant": "model",  # Gemini uses "model" for assistant turns
    "system": "user",      # Gemini doesn't have a dedicated system role in this API;
                           # prepend system content as a user turn (handled below)
}


def _convert_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Convert OpenAI-style messages to Gemini's contents format.

    Returns:
        (system_instruction, contents) where system_instruction is the text of
        any system message (mapped to Gemini's systemInstruction field), and
        contents is the list of {role, parts} dicts for the remaining messages.
    """
    system_instruction: str | None = None
    contents: list[dict] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            # Collect all system messages into a single instruction
            system_instruction = (
                (system_instruction + "\n" + content)
                if system_instruction
                else content
            )
        else:
            gemini_role = _ROLE_MAP.get(role, "user")
            contents.append({"role": gemini_role, "parts": [{"text": content}]})

    return system_instruction, contents


class GeminiClient(ProviderClient):
    """Calls Google Gemini's generateContent endpoint."""

    name = "gemini"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or get_settings().gemini_api_key

    async def complete(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        if not self._api_key:
            raise ProviderError(self.name, "GEMINI_API_KEY is not set")

        system_instruction, contents = _convert_messages(messages)

        # Gemini requires at least one content entry
        if not contents:
            raise ProviderError(self.name, "No user/assistant messages to send to Gemini")

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        url = _GEMINI_BASE_URL.format(model=model)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    url,
                    json=payload,
                    params={"key": self._api_key},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"Request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise ProviderError(self.name, f"Request error: {exc}") from exc

        if response.status_code != 200:
            raise ProviderError(
                self.name,
                f"HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )

        try:
            data = response.json()
            candidate = data["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            # Gemini returns token counts in usageMetadata
            usage = data.get("usageMetadata", {})
            input_tokens = usage.get("promptTokenCount", 0)
            output_tokens = usage.get("candidatesTokenCount", 0)
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(
                self.name, f"Malformed response body: {exc}"
            ) from exc

        logger.debug(
            "Gemini response: model=%s in=%d out=%d", model, input_tokens, output_tokens
        )
        return ProviderResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw=data,
        )
