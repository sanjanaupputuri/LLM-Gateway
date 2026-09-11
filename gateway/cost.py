# gateway/cost.py
# Computes notional cost in USD from token counts and pricing.yaml.
# Spec: docs/02_ARCHITECTURE.md §5, docs/04_BUILD_PLAN.md Phase 6
#
# Risk R-03: the lookup key is f"{provider}/{model}" — e.g.
# "groq/llama-3.1-8b-instant". Any mismatch silently returns 0.0 with
# a warning. Tests must use a non-zero-price model (Groq or Gemini) to
# verify the key lookup actually works — free OpenRouter models would
# pass vacuously.

from __future__ import annotations

import logging

from gateway.config import PricingConfig

logger = logging.getLogger(__name__)

# Tokens are priced per 1 million
_PER_MILLION = 1_000_000


def compute_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing_config: PricingConfig,
) -> float:
    """Compute the notional cost in USD for a provider response.

    Looks up the per-1M-token rates for the given provider/model pair in
    pricing_config and returns:
        (input_tokens / 1_000_000) * input_rate
      + (output_tokens / 1_000_000) * output_rate

    Returns 0.0 for any provider/model not found in the pricing table
    (logs a warning so the gap is visible in logs without crashing).

    Cache hits should always pass input_tokens=0, output_tokens=0 so that
    cost_usd=0.0 regardless of what model originally produced the cached
    response.

    Args:
        provider: Provider name, e.g. "groq", "gemini", "openrouter".
        model: Model name exactly as it appears in pricing.yaml after the
               provider prefix, e.g. "llama-3.1-8b-instant".
        input_tokens: Number of prompt tokens.
        output_tokens: Number of completion tokens.
        pricing_config: Loaded PricingConfig from config/pricing.yaml.

    Returns:
        Cost in USD as a float, rounded to 10 decimal places.
    """
    key = f"{provider}/{model}"
    pricing = pricing_config.pricing.get(key)

    if pricing is None:
        logger.warning(
            "No pricing entry for %r — returning cost_usd=0.0. "
            "Add an entry to config/pricing.yaml to track this model.",
            key,
        )
        return 0.0

    cost = (
        (input_tokens / _PER_MILLION) * pricing.input
        + (output_tokens / _PER_MILLION) * pricing.output
    )
    # Round to avoid floating-point noise in logs and DB storage
    return round(cost, 10)
