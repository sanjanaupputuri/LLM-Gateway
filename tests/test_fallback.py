# tests/test_fallback.py
# Fallback and circuit breaker tests: FB-01 through FB-07.
# Spec: docs/05_TEST_PLAN.md §3.2
#
# All provider HTTP calls are mocked with respx — no real API keys needed.
# Tests exercise the provider clients + circuit breaker directly; the router
# integration is verified here via a helper that mimics route_request() logic.

from __future__ import annotations

import json
import time

import pytest
import respx
from httpx import Response

from gateway.providers.base import ProviderError
from gateway.providers.circuit_breaker import get_breaker, reset_all_breakers
from gateway.providers.groq_client import GroqClient, _GROQ_BASE_URL
from gateway.providers.gemini_client import GeminiClient, _GEMINI_BASE_URL
from gateway.providers.openrouter_client import OpenRouterClient, _OPENROUTER_BASE_URL

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MESSAGES = [{"role": "user", "content": "Hello"}]
TEMPERATURE = 0.7
MAX_TOKENS = 64

# Minimal valid mock response bodies per provider format
def _groq_body(text: str = "Hi there", input_t: int = 5, output_t: int = 3) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": input_t, "completion_tokens": output_t},
    }

def _gemini_body(text: str = "Hi there", input_t: int = 5, output_t: int = 3) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": input_t, "candidatesTokenCount": output_t},
    }

def _openrouter_body(text: str = "Hi there", input_t: int = 5, output_t: int = 3) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": input_t, "completion_tokens": output_t},
    }


@pytest.fixture(autouse=True)
def reset_breakers():
    """Reset all circuit breakers before and after each test."""
    reset_all_breakers()
    yield
    reset_all_breakers()


@pytest.fixture
def groq() -> GroqClient:
    return GroqClient(api_key="test-groq-key")

@pytest.fixture
def gemini() -> GeminiClient:
    return GeminiClient(api_key="test-gemini-key")

@pytest.fixture
def openrouter() -> OpenRouterClient:
    return OpenRouterClient(api_key="test-openrouter-key")


# ---------------------------------------------------------------------------
# Helper: mimics the router's fallback chain logic for testing
# ---------------------------------------------------------------------------

async def _run_chain(
    clients: list,
    *,
    model_map: dict | None = None,
    max_retries: int = 1,
) -> tuple[object, str]:
    """Try each client in order, retrying once per client on ProviderError.

    Returns (ProviderResponse, provider_name) or raises ProviderError if all fail.
    """
    default_models = {
        "groq": "llama-3.1-8b-instant",
        "gemini": "gemini-2.0-flash",
        "openrouter": "meta-llama/llama-3.1-8b-instruct:free",
    }
    models = model_map or default_models

    last_error = None
    for client in clients:
        breaker = get_breaker(client.name)
        if breaker.is_open():
            continue  # skip — circuit is open

        model = models[client.name]
        attempt = 0
        while attempt <= max_retries:
            try:
                resp = await client.complete(
                    model=model,
                    messages=MESSAGES,
                    temperature=TEMPERATURE,
                    max_tokens=MAX_TOKENS,
                )
                breaker.record_success()
                return resp, client.name
            except ProviderError as exc:
                last_error = exc
                attempt += 1
                if attempt > max_retries:
                    breaker.record_failure()
                    break

    raise last_error or ProviderError("all", "All providers failed")


# ---------------------------------------------------------------------------
# FB-01: Primary succeeds — Gemini and OpenRouter never called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb01_primary_succeeds(groq, gemini, openrouter):
    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            return_value=Response(200, json=_groq_body("Hello from Groq"))
        )
        gemini_url = _GEMINI_BASE_URL.replace("{model}", "gemini-2.0-flash")
        gemini_route = mock.post(gemini_url).mock(
            return_value=Response(200, json=_gemini_body())
        )
        openrouter_route = mock.post(_OPENROUTER_BASE_URL).mock(
            return_value=Response(200, json=_openrouter_body())
        )

        resp, provider = await _run_chain([groq, gemini, openrouter])

    assert provider == "groq"
    assert resp.text == "Hello from Groq"
    assert groq_route.called
    assert not gemini_route.called
    assert not openrouter_route.called


# ---------------------------------------------------------------------------
# FB-02: Primary fails twice (1 try + 1 retry), falls over to Gemini
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb02_primary_fails_falls_to_secondary(groq, gemini, openrouter):
    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            return_value=Response(500, text="Internal Server Error")
        )
        gemini_url = _GEMINI_BASE_URL.replace("{model}", "gemini-2.0-flash")
        gemini_route = mock.post(gemini_url).mock(
            return_value=Response(200, json=_gemini_body("Hello from Gemini"))
        )

        resp, provider = await _run_chain([groq, gemini, openrouter])

    assert provider == "gemini"
    assert resp.text == "Hello from Gemini"
    # Groq should be called exactly twice (1 try + 1 retry)
    assert groq_route.call_count == 2
    assert gemini_route.call_count == 1


# ---------------------------------------------------------------------------
# FB-03: Primary and secondary fail, tertiary succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb03_tertiary_succeeds(groq, gemini, openrouter):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_GROQ_BASE_URL).mock(return_value=Response(500, text="Error"))
        gemini_url = _GEMINI_BASE_URL.replace("{model}", "gemini-2.0-flash")
        mock.post(gemini_url).mock(return_value=Response(500, text="Error"))
        openrouter_route = mock.post(_OPENROUTER_BASE_URL).mock(
            return_value=Response(200, json=_openrouter_body("Hello from OpenRouter"))
        )

        resp, provider = await _run_chain([groq, gemini, openrouter])

    assert provider == "openrouter"
    assert resp.text == "Hello from OpenRouter"
    assert openrouter_route.call_count == 1


# ---------------------------------------------------------------------------
# FB-04: All providers fail — raises ProviderError (gateway returns 502)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb04_all_providers_fail(groq, gemini, openrouter):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_GROQ_BASE_URL).mock(return_value=Response(500, text="Error"))
        gemini_url = _GEMINI_BASE_URL.replace("{model}", "gemini-2.0-flash")
        mock.post(gemini_url).mock(return_value=Response(500, text="Error"))
        mock.post(_OPENROUTER_BASE_URL).mock(return_value=Response(500, text="Error"))

        with pytest.raises(ProviderError):
            await _run_chain([groq, gemini, openrouter])


# ---------------------------------------------------------------------------
# FB-05: Circuit breaker opens after 3 consecutive failures — 4th skips Groq
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb05_circuit_breaker_opens(groq, gemini, openrouter):
    # Force the Groq breaker to open by recording 3 failures directly
    breaker = get_breaker("groq", failure_threshold=3, cooldown_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.is_open(), "Breaker should be open after 3 failures"

    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            return_value=Response(200, json=_groq_body())
        )
        gemini_url = _GEMINI_BASE_URL.replace("{model}", "gemini-2.0-flash")
        gemini_route = mock.post(gemini_url).mock(
            return_value=Response(200, json=_gemini_body("Gemini took over"))
        )

        resp, provider = await _run_chain([groq, gemini, openrouter])

    # Groq must NOT have been called — breaker was open
    assert provider == "gemini"
    assert not groq_route.called
    assert gemini_route.called


# ---------------------------------------------------------------------------
# FB-06: Half-open recovery — after cooldown, trial succeeds, breaker closes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb06_half_open_recovery(groq, gemini, openrouter):
    from gateway.providers.circuit_breaker import BreakerState

    # Force breaker open then manually backdate opened_at past the cooldown
    breaker = get_breaker("groq", failure_threshold=3, cooldown_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open()

    # Backdate opened_at so cooldown has elapsed
    breaker._opened_at = time.monotonic() - 61  # 61s ago

    # is_open() will now transition to HALF_OPEN internally
    assert not breaker.is_open(), "Breaker should be half-open (allows one trial)"
    assert breaker.state == BreakerState.HALF_OPEN

    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            return_value=Response(200, json=_groq_body("Groq recovered"))
        )

        resp, provider = await _run_chain([groq, gemini, openrouter])

    assert provider == "groq"
    assert resp.text == "Groq recovered"
    assert groq_route.called
    assert breaker.state == BreakerState.CLOSED, "Breaker should be closed after successful trial"


# ---------------------------------------------------------------------------
# FB-07: Timeout — Groq hangs, fails over to Gemini within bounded time
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb07_timeout_failover(groq, gemini, openrouter):
    import httpx

    with respx.mock(assert_all_called=False) as mock:
        # Make Groq raise a TimeoutException
        mock.post(_GROQ_BASE_URL).mock(side_effect=httpx.TimeoutException("timeout"))
        gemini_url = _GEMINI_BASE_URL.replace("{model}", "gemini-2.0-flash")
        gemini_route = mock.post(gemini_url).mock(
            return_value=Response(200, json=_gemini_body("Gemini after timeout"))
        )

        start = time.monotonic()
        resp, provider = await _run_chain([groq, gemini, openrouter])
        elapsed = time.monotonic() - start

    assert provider == "gemini"
    assert resp.text == "Gemini after timeout"
    # Should complete well within 9.5s (mocked timeout is instant)
    assert elapsed < 9.5, f"Took too long: {elapsed:.2f}s"
    assert gemini_route.called
