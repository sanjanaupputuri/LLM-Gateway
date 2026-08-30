# tests/test_routing.py
# Routing correctness tests: RT-01 through RT-05, plus route_request() integration.
# Spec: docs/05_TEST_PLAN.md §3.1, docs/04_BUILD_PLAN.md Phase 3

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response

from gateway.config import get_routing_rules, clear_config_cache
from gateway.providers.base import ProviderError, ProviderResponse
from gateway.providers.circuit_breaker import reset_all_breakers
from gateway.router import AllProvidersFailedError, classify, get_chain, route_request

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RULES_PATH = "config/routing_rules.yaml"

@pytest.fixture
def rules():
    """Load real routing rules from config/routing_rules.yaml."""
    return get_routing_rules(RULES_PATH)


@pytest.fixture(autouse=True)
def reset_breakers():
    """Reset all circuit breakers before and after each test."""
    reset_all_breakers()
    yield
    reset_all_breakers()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_messages(content: str) -> list[dict]:
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------------------
# RT-01: Short, plain prompt → "simple"
# ---------------------------------------------------------------------------

def test_rt01_short_plain_prompt_is_simple(rules):
    """A short, keyword-free prompt should classify as 'simple'."""
    messages = _make_messages("What's the capital of France?")
    assert classify(messages, rules) == "simple"


# ---------------------------------------------------------------------------
# RT-02: Long prompt (>400 chars, no keywords) → "complex"
# ---------------------------------------------------------------------------

def test_rt02_long_prompt_is_complex(rules):
    """A prompt exceeding max_simple_chars should classify as 'complex'."""
    # Build a 500-char prompt with no complex keywords
    long_content = "Tell me about the history of the Roman Empire. " * 12  # ~564 chars
    long_content = long_content[:500]
    assert len(long_content) > rules.classifier.max_simple_chars, (
        f"Test prompt is only {len(long_content)} chars — must exceed "
        f"{rules.classifier.max_simple_chars}"
    )
    messages = _make_messages(long_content)
    assert classify(messages, rules) == "complex"


# ---------------------------------------------------------------------------
# RT-03: Keyword trigger (short prompt) → "complex"
# ---------------------------------------------------------------------------

def test_rt03_keyword_analyze_is_complex(rules):
    """A short prompt containing a complex keyword should classify as 'complex'."""
    messages = _make_messages("Can you analyze this dataset?")
    assert classify(messages, rules) == "complex"


@pytest.mark.parametrize("keyword", [
    "step by step",
    "analyze",
    "architecture",
    "code review",
    "compare",
    "explain in detail",
    "write a",
    "debug",
])
def test_rt03_all_complex_keywords(rules, keyword):
    """Every complex keyword in routing_rules.yaml triggers 'complex'."""
    messages = _make_messages(f"Please {keyword} this for me.")
    assert classify(messages, rules) == "complex"


def test_rt03_keyword_case_insensitive(rules):
    """Keyword matching must be case-insensitive."""
    messages = _make_messages("ANALYZE this document please")
    assert classify(messages, rules) == "complex"


# ---------------------------------------------------------------------------
# RT-04: Chain lookup — simple tier
# ---------------------------------------------------------------------------

def test_rt04_simple_chain(rules):
    """Simple-tier chain must match routing_rules.yaml exactly."""
    chain = get_chain("simple", rules)
    assert len(chain) == 3
    assert chain[0].provider == "groq"
    assert chain[0].model == "llama-3.1-8b-instant"
    assert chain[1].provider == "gemini"
    assert chain[1].model == "gemini-2.0-flash"
    assert chain[2].provider == "openrouter"
    assert chain[2].model == "meta-llama/llama-3.1-8b-instruct:free"


# ---------------------------------------------------------------------------
# RT-05: Chain lookup — complex tier
# ---------------------------------------------------------------------------

def test_rt05_complex_chain_starts_with_groq_70b(rules):
    """Complex-tier chain must start with groq/llama-3.3-70b-versatile."""
    chain = get_chain("complex", rules)
    assert len(chain) == 3
    assert chain[0].provider == "groq"
    assert chain[0].model == "llama-3.3-70b-versatile"
    assert chain[1].provider == "gemini"
    assert chain[2].provider == "openrouter"


# ---------------------------------------------------------------------------
# classify: edge cases
# ---------------------------------------------------------------------------

def test_classify_uses_last_user_message(rules):
    """classify() must inspect the LAST user message, not all messages."""
    messages = [
        {"role": "user", "content": "analyze everything"},   # complex keyword
        {"role": "assistant", "content": "Sure!"},
        {"role": "user", "content": "OK thanks"},            # plain, short — last
    ]
    # Last user message is plain/short, so should be simple
    assert classify(messages, rules) == "simple"


def test_classify_no_user_message(rules):
    """classify() with no user messages should default to 'simple'."""
    messages = [{"role": "system", "content": "You are a helpful assistant."}]
    assert classify(messages, rules) == "simple"


def test_classify_empty_messages(rules):
    """classify() with an empty list should default to 'simple'."""
    assert classify([], rules) == "simple"


def test_classify_exactly_at_boundary(rules):
    """A prompt exactly at max_simple_chars should be 'simple' (not strictly greater)."""
    boundary = rules.classifier.max_simple_chars
    content = "x" * boundary  # exactly at threshold
    assert classify(_make_messages(content), rules) == "simple"


def test_classify_one_over_boundary(rules):
    """A prompt one character over max_simple_chars must be 'complex'."""
    boundary = rules.classifier.max_simple_chars
    content = "x" * (boundary + 1)
    assert classify(_make_messages(content), rules) == "complex"


# ---------------------------------------------------------------------------
# route_request: integration tests (mocked provider clients)
# ---------------------------------------------------------------------------

def _mock_client(name: str, *, success: bool = True, text: str = "ok") -> MagicMock:
    """Build a mock ProviderClient that either succeeds or raises ProviderError."""
    client = MagicMock()
    client.name = name
    if success:
        client.complete = AsyncMock(
            return_value=ProviderResponse(
                text=text,
                input_tokens=10,
                output_tokens=5,
                raw={},
            )
        )
    else:
        client.complete = AsyncMock(
            side_effect=ProviderError(name, "simulated failure")
        )
    return client


@pytest.mark.asyncio
async def test_route_request_primary_succeeds(rules):
    """route_request returns from the first provider when it succeeds."""
    groq = _mock_client("groq", text="Hello from Groq")
    gemini = _mock_client("gemini")
    openrouter = _mock_client("openrouter")

    clients = {"groq": groq, "gemini": gemini, "openrouter": openrouter}
    messages = _make_messages("What is 2+2?")

    resp, provider, model = await route_request(
        messages=messages,
        temperature=0.7,
        max_tokens=64,
        rules=rules,
        provider_clients=clients,
    )

    assert provider == "groq"
    assert resp.text == "Hello from Groq"
    groq.complete.assert_called_once()
    gemini.complete.assert_not_called()
    openrouter.complete.assert_not_called()


@pytest.mark.asyncio
async def test_route_request_falls_back_to_secondary(rules):
    """route_request falls back to gemini when groq fails."""
    groq = _mock_client("groq", success=False)
    gemini = _mock_client("gemini", text="Hello from Gemini")
    openrouter = _mock_client("openrouter")

    clients = {"groq": groq, "gemini": gemini, "openrouter": openrouter}
    messages = _make_messages("What is 2+2?")

    resp, provider, model = await route_request(
        messages=messages,
        temperature=0.7,
        max_tokens=64,
        rules=rules,
        provider_clients=clients,
    )

    assert provider == "gemini"
    assert resp.text == "Hello from Gemini"
    groq.complete.assert_called_once()
    gemini.complete.assert_called_once()
    openrouter.complete.assert_not_called()


@pytest.mark.asyncio
async def test_route_request_all_fail_raises(rules):
    """route_request raises AllProvidersFailedError when the whole chain fails."""
    groq = _mock_client("groq", success=False)
    gemini = _mock_client("gemini", success=False)
    openrouter = _mock_client("openrouter", success=False)

    clients = {"groq": groq, "gemini": gemini, "openrouter": openrouter}
    messages = _make_messages("What is 2+2?")

    with pytest.raises(AllProvidersFailedError):
        await route_request(
            messages=messages,
            temperature=0.7,
            max_tokens=64,
            rules=rules,
            provider_clients=clients,
        )


@pytest.mark.asyncio
async def test_route_request_skips_open_breaker(rules):
    """route_request skips a provider whose circuit breaker is open."""
    from gateway.providers.circuit_breaker import get_breaker

    # Force groq breaker open
    breaker = get_breaker(
        "groq",
        failure_threshold=rules.circuit_breaker.failure_threshold,
        cooldown_seconds=rules.circuit_breaker.cooldown_seconds,
    )
    for _ in range(rules.circuit_breaker.failure_threshold):
        breaker.record_failure()

    assert breaker.is_open()

    groq = _mock_client("groq", text="Should not be called")
    gemini = _mock_client("gemini", text="Gemini to the rescue")
    openrouter = _mock_client("openrouter")

    clients = {"groq": groq, "gemini": gemini, "openrouter": openrouter}
    messages = _make_messages("Hello")

    resp, provider, model = await route_request(
        messages=messages,
        temperature=0.7,
        max_tokens=64,
        rules=rules,
        provider_clients=clients,
    )

    assert provider == "gemini"
    groq.complete.assert_not_called()
    gemini.complete.assert_called_once()


@pytest.mark.asyncio
async def test_route_request_complex_prompt_uses_complex_chain(rules):
    """A complex prompt is served by the complex-tier chain (groq 70b model)."""
    groq = _mock_client("groq", text="Complex answer")
    gemini = _mock_client("gemini")
    openrouter = _mock_client("openrouter")

    clients = {"groq": groq, "gemini": gemini, "openrouter": openrouter}
    # "analyze" keyword triggers complex classification
    messages = _make_messages("Can you analyze the performance bottlenecks?")

    resp, provider, model = await route_request(
        messages=messages,
        temperature=0.7,
        max_tokens=256,
        rules=rules,
        provider_clients=clients,
    )

    assert provider == "groq"
    assert model == "llama-3.3-70b-versatile"  # complex-tier model


@pytest.mark.asyncio
async def test_route_request_records_failure_on_breaker(rules):
    """A ProviderError causes record_failure() to be called on that provider's breaker."""
    from gateway.providers.circuit_breaker import get_breaker, BreakerState

    groq = _mock_client("groq", success=False)
    gemini = _mock_client("gemini", text="Fallback")
    openrouter = _mock_client("openrouter")

    clients = {"groq": groq, "gemini": gemini, "openrouter": openrouter}

    await route_request(
        messages=_make_messages("Hello"),
        temperature=0.7,
        max_tokens=64,
        rules=rules,
        provider_clients=clients,
    )

    groq_breaker = get_breaker("groq")
    # One failure recorded — breaker still closed (threshold is 3)
    assert groq_breaker._failure_count == 1
    assert groq_breaker.state == BreakerState.CLOSED
