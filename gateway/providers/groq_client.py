# gateway/providers/groq_client.py
# Groq provider client — calls the OpenAI-compatible Groq API.
# Endpoint: https://api.groq.com/openai/v1/chat/completions
# Spec: docs/02_ARCHITECTURE.md §6

from __future__ import annotations

import logging

import httpx

from gateway.config import get_settings
from gateway.providers.base import ProviderClient, ProviderError, ProviderResponse

logger = logging.getLogger(__name__)

_GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
_TIMEOUT = 8.0  # seconds — per spec


class GroqClient(ProviderClient):
    """Calls Groq's OpenAI-compatible chat completions endpoint."""

    name = "groq"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or get_settings().groq_api_key

    async def complete(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        if not self._api_key:
            raise ProviderError(self.name, "GROQ_API_KEY is not set")

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    _GROQ_BASE_URL,
                    json=payload,
                    headers=headers,
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
            choice = data["choices"][0]
            text = choice["message"]["content"]
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(
                self.name, f"Malformed response body: {exc}"
            ) from exc

        logger.debug("Groq response: model=%s in=%d out=%d", model, input_tokens, output_tokens)
        return ProviderResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw=data,
        )
