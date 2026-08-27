# gateway/providers/base.py
# Abstract base class and shared data types for all provider clients.
# Spec: docs/02_ARCHITECTURE.md §6

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class ProviderResponse(BaseModel):
    """Normalized response from any provider — the rest of the system only sees this."""
    text: str
    input_tokens: int
    output_tokens: int
    raw: dict  # original provider response payload, for debugging


class ProviderError(Exception):
    """Raised by any ProviderClient on timeout, non-2xx response, or malformed body.

    Never let a raw httpx or provider-specific exception escape a client —
    always catch and re-raise as ProviderError so the router can handle it
    uniformly regardless of which provider failed.
    """

    def __init__(self, provider: str, message: str, status_code: int | None = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"[{provider}] {message} (status={status_code})")


class ProviderClient(ABC):
    """Abstract base for Groq, Gemini, and OpenRouter clients.

    Each subclass must:
    - Set a class-level `name` attribute matching its key in routing_rules.yaml
      (e.g. "groq", "gemini", "openrouter").
    - Implement `complete()`, raising ProviderError on any failure.
    - Set an 8-second httpx timeout on all outbound calls.
    - Never let raw exceptions escape — always wrap in ProviderError.
    """

    name: str  # "groq" | "gemini" | "openrouter"

    @abstractmethod
    async def complete(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        """Call the provider and return a normalized ProviderResponse.

        Args:
            model: Provider-specific model name (e.g. "llama-3.1-8b-instant").
            messages: OpenAI-style message list [{"role": ..., "content": ...}].
            temperature: Sampling temperature (0.0–2.0).
            max_tokens: Maximum tokens in the completion.

        Returns:
            ProviderResponse with text, token counts, and raw payload.

        Raises:
            ProviderError: On any failure — timeout, non-2xx, rate limit, malformed body.
        """
