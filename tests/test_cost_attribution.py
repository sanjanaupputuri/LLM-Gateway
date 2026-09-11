# tests/test_cost_attribution.py
# Cost attribution tests: CO-01 through CO-08.
# Spec: docs/04_BUILD_PLAN.md Phase 6, docs/02_ARCHITECTURE.md §5
#
# All tests use the real pricing.yaml via get_pricing() so that any future
# pricing change is automatically caught.
#
# IMPORTANT (R-03): tests CO-01 and CO-02 deliberately use non-zero-price
# models (groq/llama-3.1-8b-instant and gemini/gemini-2.0-flash). Testing
# only with free OpenRouter models would pass vacuously since cost_usd=0.0
# regardless of whether the key lookup actually works.

from __future__ import annotations

import pytest

from gateway.config import get_pricing
from gateway.cost import compute_cost

PRICING_PATH = "config/pricing.yaml"


@pytest.fixture
def pricing():
    return get_pricing(PRICING_PATH)


# ---------------------------------------------------------------------------
# CO-01: Groq llama-3.1-8b-instant — non-zero price, verifies key lookup
# ---------------------------------------------------------------------------

def test_co01_groq_8b_correct_cost(pricing):
    """compute_cost returns the exact expected value for groq/llama-3.1-8b-instant.

    Pricing: input=$0.05/1M, output=$0.08/1M
    Inputs: 1_000_000 input tokens, 1_000_000 output tokens
    Expected: 0.05 + 0.08 = 0.13 USD
    """
    cost = compute_cost(
        provider="groq",
        model="llama-3.1-8b-instant",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing_config=pricing,
    )
    assert cost == pytest.approx(0.13, rel=1e-6)


def test_co01_groq_8b_small_counts(pricing):
    """Verify the per-token math at realistic token counts for groq 8B.

    142 input tokens, 87 output tokens.
    Expected: (142/1M)*0.05 + (87/1M)*0.08
    """
    expected = (142 / 1_000_000) * 0.05 + (87 / 1_000_000) * 0.08
    cost = compute_cost("groq", "llama-3.1-8b-instant", 142, 87, pricing)
    assert cost == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# CO-02: Groq llama-3.3-70b-versatile — higher price, different model
# ---------------------------------------------------------------------------

def test_co02_groq_70b_correct_cost(pricing):
    """compute_cost returns correct value for groq/llama-3.3-70b-versatile.

    Pricing: input=$0.59/1M, output=$0.79/1M
    """
    cost = compute_cost(
        provider="groq",
        model="llama-3.3-70b-versatile",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing_config=pricing,
    )
    assert cost == pytest.approx(0.59 + 0.79, rel=1e-6)


# ---------------------------------------------------------------------------
# CO-03: Gemini 2.0 Flash — verifies non-OpenAI-compatible provider pricing
# ---------------------------------------------------------------------------

def test_co03_gemini_flash_correct_cost(pricing):
    """compute_cost returns correct value for gemini/gemini-2.0-flash.

    Pricing: input=$0.10/1M, output=$0.40/1M
    """
    cost = compute_cost(
        provider="gemini",
        model="gemini-2.0-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing_config=pricing,
    )
    assert cost == pytest.approx(0.10 + 0.40, rel=1e-6)


# ---------------------------------------------------------------------------
# CO-04: Free OpenRouter models — cost is exactly 0.0
# ---------------------------------------------------------------------------

def test_co04_openrouter_free_llama_zero_cost(pricing):
    """Free OpenRouter model returns 0.0 regardless of token counts."""
    cost = compute_cost(
        provider="openrouter",
        model="meta-llama/llama-3.1-8b-instruct:free",
        input_tokens=500_000,
        output_tokens=500_000,
        pricing_config=pricing,
    )
    assert cost == 0.0


def test_co04_openrouter_free_deepseek_zero_cost(pricing):
    """Free deepseek model on OpenRouter returns 0.0."""
    cost = compute_cost(
        provider="openrouter",
        model="deepseek/deepseek-chat:free",
        input_tokens=500_000,
        output_tokens=500_000,
        pricing_config=pricing,
    )
    assert cost == 0.0


# ---------------------------------------------------------------------------
# CO-05: Zero tokens — cost is always 0.0 regardless of price
# ---------------------------------------------------------------------------

def test_co05_zero_tokens_zero_cost(pricing):
    """Zero input and output tokens always produce 0.0 cost (used for cache hits)."""
    cost = compute_cost("groq", "llama-3.3-70b-versatile", 0, 0, pricing)
    assert cost == 0.0


# ---------------------------------------------------------------------------
# CO-06: Unknown model returns 0.0 with no crash
# ---------------------------------------------------------------------------

def test_co06_unknown_model_returns_zero(pricing):
    """An unknown provider/model key returns 0.0 without raising."""
    cost = compute_cost("unknown_provider", "unknown_model", 1000, 500, pricing)
    assert cost == 0.0


def test_co06_unknown_model_for_known_provider_returns_zero(pricing):
    """A known provider with an unknown model name returns 0.0."""
    cost = compute_cost("groq", "nonexistent-model-xyz", 1000, 500, pricing)
    assert cost == 0.0


# ---------------------------------------------------------------------------
# CO-07: Input-only or output-only token scenarios
# ---------------------------------------------------------------------------

def test_co07_input_only(pricing):
    """Only input tokens produce cost proportional to the input rate."""
    cost = compute_cost("groq", "llama-3.1-8b-instant", 1_000_000, 0, pricing)
    assert cost == pytest.approx(0.05, rel=1e-6)


def test_co07_output_only(pricing):
    """Only output tokens produce cost proportional to the output rate."""
    cost = compute_cost("groq", "llama-3.1-8b-instant", 0, 1_000_000, pricing)
    assert cost == pytest.approx(0.08, rel=1e-6)


# ---------------------------------------------------------------------------
# CO-08: Cost is always non-negative
# ---------------------------------------------------------------------------

def test_co08_cost_is_non_negative(pricing):
    """compute_cost must never return a negative value."""
    for provider, model in [
        ("groq", "llama-3.1-8b-instant"),
        ("groq", "llama-3.3-70b-versatile"),
        ("gemini", "gemini-2.0-flash"),
        ("openrouter", "meta-llama/llama-3.1-8b-instruct:free"),
    ]:
        cost = compute_cost(provider, model, 100, 100, pricing)
        assert cost >= 0.0, f"Negative cost for {provider}/{model}: {cost}"
