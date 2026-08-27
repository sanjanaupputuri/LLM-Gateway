# gateway/providers/openrouter_client.py
# OpenRouter provider client — OpenAI-compatible API.
# Endpoint: https://openrouter.ai/api/v1/chat/completions
# Spec: docs/02_ARCHITECTURE.md §6

from __future__ import annotations

import logging

import httpx

from gateway.config import get_settings
from gateway.providers.base import ProviderClient, ProviderError, ProviderResponse

logger = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
_TIMEOUT = 8.0  # seconds — per spec


class OpenRouterClient(ProviderClient):
    """Calls OpenRouter's OpenAI-compatible chat completions endpoint.

    OpenRouter requires an HTTP-Referer header for free-tier models.
    We set it to a stable identifier for this gateway project.
    """

    name = "openrouter"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or get_settings().openrouter_api_key

    async def complete(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        if not self._api_key:
            raise ProviderError(self.name, "OPENROUTER_API_KEY is not set")

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # Required by OpenRouter for free-tier usage tracking
            "HTTP-Referer": "https://github.com/llm-gateway",
            "X-Title": "LLM Gateway",
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    _OPENROUTER_BASE_URL,
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
            # Check for OpenRouter-specific error in body (sometimes 200 with error key)
            if "error" in data:
                raise ProviderError(
                    self.name,
                    f"API error in response body: {data['error']}",
                    status_code=200,
                )
            choice = data["choices"][0]
            text = choice["message"]["content"]
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
        except ProviderError:
            raise
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(
                self.name, f"Malformed response body: {exc}"
            ) from exc

        logger.debug(
            "OpenRouter response: model=%s in=%d out=%d", model, input_tokens, output_tokens
        )
        return ProviderResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw=data,
        )
