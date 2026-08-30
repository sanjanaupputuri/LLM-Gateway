# gateway/router.py
# Routing engine: classify(), get_chain(), and route_request().
# Spec: docs/02_ARCHITECTURE.md §8, docs/04_BUILD_PLAN.md Phase 3

from __future__ import annotations

import logging
from typing import Literal

from gateway.config import RoutingRules, RouteStep
from gateway.providers.base import ProviderClient, ProviderError, ProviderResponse
from gateway.providers.circuit_breaker import get_breaker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class AllProvidersFailedError(Exception):
    """Raised by route_request() when every step in the chain has been
    exhausted — either due to open circuit breakers or ProviderErrors."""

    def __init__(self, tier: str, errors: list[ProviderError]) -> None:
        self.tier = tier
        self.errors = errors
        details = "; ".join(str(e) for e in errors) if errors else "all skipped (open breakers)"
        super().__init__(f"All providers failed for tier '{tier}': {details}")


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def classify(
    messages: list[dict],
    rules: RoutingRules,
) -> Literal["simple", "complex"]:
    """Classify a request as 'simple' or 'complex'.

    A request is 'complex' if EITHER:
    - The last user message is longer than rules.classifier.max_simple_chars, OR
    - The last user message contains any of rules.classifier.complex_keywords
      (case-insensitive).

    Otherwise it is 'simple'.

    Args:
        messages: OpenAI-style message list [{"role": ..., "content": ...}].
        rules: Loaded RoutingRules from config.

    Returns:
        "simple" or "complex".
    """
    # Find the last message with role == "user"
    last_user_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_content = msg.get("content", "")
            break

    # Length check
    if len(last_user_content) > rules.classifier.max_simple_chars:
        logger.debug(
            "classify -> complex (length %d > %d)",
            len(last_user_content),
            rules.classifier.max_simple_chars,
        )
        return "complex"

    # Keyword check (case-insensitive)
    lowered = last_user_content.lower()
    for kw in rules.classifier.complex_keywords:
        if kw.lower() in lowered:
            logger.debug("classify -> complex (keyword %r found)", kw)
            return "complex"

    logger.debug("classify -> simple")
    return "simple"


# ---------------------------------------------------------------------------
# get_chain
# ---------------------------------------------------------------------------

def get_chain(
    tier: Literal["simple", "complex"],
    rules: RoutingRules,
) -> list[RouteStep]:
    """Return the ordered list of RouteStep objects for the given tier.

    Args:
        tier: "simple" or "complex".
        rules: Loaded RoutingRules from config.

    Returns:
        List of RouteStep(provider, model) in fallback order.
    """
    if tier == "simple":
        return list(rules.routes.simple.chain)
    return list(rules.routes.complex.chain)


# ---------------------------------------------------------------------------
# route_request
# ---------------------------------------------------------------------------

async def route_request(
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    rules: RoutingRules,
    provider_clients: dict[str, ProviderClient],
) -> tuple[ProviderResponse, str, str]:
    """Classify the request, pick a provider chain, and attempt each step
    in order until one succeeds or all are exhausted.

    Circuit-breaker integration:
    - Any provider whose breaker is_open() is skipped immediately (no call made).
    - On ProviderError, record_failure() is called on that provider's breaker.
    - On success, record_success() is called and the function returns.

    Args:
        messages: OpenAI-style message list.
        temperature: Sampling temperature.
        max_tokens: Max tokens to generate.
        rules: Loaded RoutingRules.
        provider_clients: Mapping of provider name -> ProviderClient instance.

    Returns:
        (ProviderResponse, provider_name, model_name)

    Raises:
        AllProvidersFailedError: When every step in the chain has failed or
            was skipped.
    """
    tier = classify(messages, rules)
    chain = get_chain(tier, rules)

    errors: list[ProviderError] = []

    for step in chain:
        provider_name = step.provider
        model = step.model

        # Look up the client; skip if not registered
        client = provider_clients.get(provider_name)
        if client is None:
            logger.warning(
                "No client registered for provider %r — skipping step.", provider_name
            )
            continue

        # Skip if circuit breaker is open
        breaker = get_breaker(
            provider_name,
            failure_threshold=rules.circuit_breaker.failure_threshold,
            cooldown_seconds=rules.circuit_breaker.cooldown_seconds,
        )
        if breaker.is_open():
            logger.info(
                "Circuit breaker OPEN for %r — skipping.", provider_name
            )
            continue

        # Attempt the call
        logger.info(
            "Attempting provider=%r model=%r tier=%r", provider_name, model, tier
        )
        try:
            response = await client.complete(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            breaker.record_success()
            logger.info(
                "Success: provider=%r model=%r in=%d out=%d",
                provider_name,
                model,
                response.input_tokens,
                response.output_tokens,
            )
            return response, provider_name, model

        except ProviderError as exc:
            logger.warning(
                "ProviderError from %r: %s — recording failure.", provider_name, exc
            )
            breaker.record_failure()
            errors.append(exc)

    raise AllProvidersFailedError(tier=tier, errors=errors)
