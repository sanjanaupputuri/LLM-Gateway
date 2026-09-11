# tests/test_fallback.py
# Fallback and circuit breaker tests: FB-01 through FB-07.
# Spec: docs/05_TEST_PLAN.md §3.2
#
# All provider HTTP calls are mocked with respx — no real API keys needed.
# Tests exercise the full route_request() path from gateway/router.py,
# including the retry-before-fallback behaviour added in Phase 3 (FR3).
#
# Note on call counts:
#   route_request() retries each provider MAX_RETRIES+1 times (currently 2)
#   before advancing the chain.  Tests that expect a provider to fail assert
#   call_count == MAX_RETRIES + 1 (i.e. 2), not 1.

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response

from gateway.config import get_routing_rules
from gateway.providers.base import ProviderError, ProviderResponse
from gateway.providers.circuit_breaker import get_breaker, reset_all_breakers
from gateway.providers.groq_client import GroqClient, _GROQ_BASE_URL
from gateway.providers.gemini_client import GeminiClient, _GEMINI_BASE_URL
from gateway.providers.openrouter_client import OpenRouterClient, _OPENROUTER_BASE_URL
from gateway.router import MAX_RETRIES, AllProvidersFailedError, route_request

# ---------------------------------------------------------------------------
# Constants shared across tests
# ---------------------------------------------------------------------------

RULES_PATH = "config/routing_rules.yaml"
MESSAGES = [{"role": "user", "content": "Hello"}]
TEMPERATURE = 0.7
MAX_TOKENS = 64


# ---------------------------------------------------------------------------
# Minimal valid mock response bodies per provider format
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def rules():
    return get_routing_rules(RULES_PATH)


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


@pytest.fixture
def clients(groq, gemini, openrouter) -> dict:
    return {"groq": groq, "gemini": gemini, "openrouter": openrouter}


# Gemini URL for the simple-tier model
_GEMINI_FLASH_URL = _GEMINI_BASE_URL.replace("{model}", "gemini-2.0-flash")


# ---------------------------------------------------------------------------
# FB-01: Primary succeeds — Gemini and OpenRouter never called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb01_primary_succeeds(rules, clients):
    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            return_value=Response(200, json=_groq_body("Hello from Groq"))
        )
        gemini_route = mock.post(_GEMINI_FLASH_URL).mock(
            return_value=Response(200, json=_gemini_body())
        )
        openrouter_route = mock.post(_OPENROUTER_BASE_URL).mock(
            return_value=Response(200, json=_openrouter_body())
        )

        resp, provider, model = await route_request(
            messages=MESSAGES,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            rules=rules,
            provider_clients=clients,
        )

    assert provider == "groq"
    assert resp.text == "Hello from Groq"
    # Succeeded on first attempt — exactly 1 call to groq
    assert groq_route.call_count == 1
    assert not gemini_route.called
    assert not openrouter_route.called


# ---------------------------------------------------------------------------
# FB-02: Primary fails on all retries, falls over to Gemini
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb02_primary_fails_falls_to_secondary(rules, clients):
    """Groq fails every attempt; after MAX_RETRIES+1 tries router falls to Gemini."""
    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            return_value=Response(500, text="Internal Server Error")
        )
        gemini_route = mock.post(_GEMINI_FLASH_URL).mock(
            return_value=Response(200, json=_gemini_body("Hello from Gemini"))
        )

        resp, provider, model = await route_request(
            messages=MESSAGES,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            rules=rules,
            provider_clients=clients,
        )

    assert provider == "gemini"
    assert resp.text == "Hello from Gemini"
    # Groq must be called MAX_RETRIES+1 times (initial + retries) before fallback
    assert groq_route.call_count == MAX_RETRIES + 1
    assert gemini_route.call_count == 1


# ---------------------------------------------------------------------------
# FB-03: Primary and secondary fail on all retries, tertiary succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb03_tertiary_succeeds(rules, clients):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_GROQ_BASE_URL).mock(return_value=Response(500, text="Error"))
        mock.post(_GEMINI_FLASH_URL).mock(return_value=Response(500, text="Error"))
        openrouter_route = mock.post(_OPENROUTER_BASE_URL).mock(
            return_value=Response(200, json=_openrouter_body("Hello from OpenRouter"))
        )

        resp, provider, model = await route_request(
            messages=MESSAGES,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            rules=rules,
            provider_clients=clients,
        )

    assert provider == "openrouter"
    assert resp.text == "Hello from OpenRouter"
    assert openrouter_route.call_count == 1


# ---------------------------------------------------------------------------
# FB-04: All providers fail all retries — raises AllProvidersFailedError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb04_all_providers_fail(rules, clients):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_GROQ_BASE_URL).mock(return_value=Response(500, text="Error"))
        mock.post(_GEMINI_FLASH_URL).mock(return_value=Response(500, text="Error"))
        mock.post(_OPENROUTER_BASE_URL).mock(return_value=Response(500, text="Error"))

        with pytest.raises(AllProvidersFailedError):
            await route_request(
                messages=MESSAGES,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                rules=rules,
                provider_clients=clients,
            )


# ---------------------------------------------------------------------------
# FB-05: Circuit breaker opens after threshold failures — next call skips Groq
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb05_circuit_breaker_opens(rules, clients):
    # Force the Groq breaker open by recording failures directly
    breaker = get_breaker(
        "groq",
        failure_threshold=rules.circuit_breaker.failure_threshold,
        cooldown_seconds=rules.circuit_breaker.cooldown_seconds,
    )
    for _ in range(rules.circuit_breaker.failure_threshold):
        breaker.record_failure()

    assert breaker.is_open(), "Breaker should be open after threshold failures"

    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            return_value=Response(200, json=_groq_body())
        )
        gemini_route = mock.post(_GEMINI_FLASH_URL).mock(
            return_value=Response(200, json=_gemini_body("Gemini took over"))
        )

        resp, provider, model = await route_request(
            messages=MESSAGES,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            rules=rules,
            provider_clients=clients,
        )

    # Groq must NOT have been called — breaker was open
    assert provider == "gemini"
    assert not groq_route.called
    assert gemini_route.called


# ---------------------------------------------------------------------------
# FB-06: Half-open recovery — after cooldown, trial succeeds, breaker closes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb06_half_open_recovery(rules, clients):
    from gateway.providers.circuit_breaker import BreakerState

    breaker = get_breaker(
        "groq",
        failure_threshold=rules.circuit_breaker.failure_threshold,
        cooldown_seconds=rules.circuit_breaker.cooldown_seconds,
    )
    for _ in range(rules.circuit_breaker.failure_threshold):
        breaker.record_failure()
    assert breaker.is_open()

    # Backdate opened_at so cooldown has elapsed
    breaker._opened_at = time.monotonic() - (rules.circuit_breaker.cooldown_seconds + 1)

    # is_open() will now see elapsed > cooldown and transition to HALF_OPEN
    assert not breaker.is_open(), "Breaker should be half-open (allows one trial)"
    assert breaker.state == BreakerState.HALF_OPEN

    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            return_value=Response(200, json=_groq_body("Groq recovered"))
        )

        resp, provider, model = await route_request(
            messages=MESSAGES,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            rules=rules,
            provider_clients=clients,
        )

    assert provider == "groq"
    assert resp.text == "Groq recovered"
    assert groq_route.called
    assert breaker.state == BreakerState.CLOSED, "Breaker should be closed after successful trial"


# ---------------------------------------------------------------------------
# FB-07: Timeout — Groq hangs on all retries, fails over to Gemini
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb07_timeout_failover(rules, clients):
    import httpx

    with respx.mock(assert_all_called=False) as mock:
        groq_route = mock.post(_GROQ_BASE_URL).mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        gemini_route = mock.post(_GEMINI_FLASH_URL).mock(
            return_value=Response(200, json=_gemini_body("Gemini after timeout"))
        )

        start = time.monotonic()
        resp, provider, model = await route_request(
            messages=MESSAGES,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            rules=rules,
            provider_clients=clients,
        )
        elapsed = time.monotonic() - start

    assert provider == "gemini"
    assert resp.text == "Gemini after timeout"
    # Groq is retried MAX_RETRIES+1 times before giving up (mocked so instant)
    assert groq_route.call_count == MAX_RETRIES + 1
    assert gemini_route.called
    # All mocked — should complete well within 1s regardless of retry count
    assert elapsed < 9.5, f"Took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# FB-08: Retry succeeds on second attempt — no fallback needed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fb08_retry_succeeds_no_fallback(rules, clients):
    """If the first attempt fails but the retry succeeds, no fallback occurs."""
    call_count = 0

    async def flaky_complete(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ProviderError("groq", "transient error")
        return ProviderResponse(text="Groq on retry", input_tokens=5, output_tokens=3, raw={})

    clients["groq"].complete = flaky_complete

    with respx.mock(assert_all_called=False) as mock:
        gemini_route = mock.post(_GEMINI_FLASH_URL).mock(
            return_value=Response(200, json=_gemini_body())
        )

        resp, provider, model = await route_request(
            messages=MESSAGES,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            rules=rules,
            provider_clients=clients,
        )

    assert provider == "groq"
    assert resp.text == "Groq on retry"
    assert call_count == 2  # failed once, succeeded on retry
    assert not gemini_route.called  # no fallback needed
