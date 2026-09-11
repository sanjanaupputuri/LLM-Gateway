# gateway/routes/chat.py
# POST /v1/chat/completions — core gateway endpoint.
# Spec: docs/03_API_CONTRACTS.md, docs/02_ARCHITECTURE.md §10
#
# Order of operations (per §10):
#   1. Start latency timer
#   2. Resolve team from X-Team-Key (401 on failure — logged)
#   3. Check RPM (429 rate_limited — logged)
#   4. Check daily token budget (429 quota_exceeded — logged)
#   5. Exact cache lookup (200 cache hit — logged, no provider call)
#   6. Semantic cache lookup (200 cache hit — logged, no provider call)
#   7. route_request() — provider chain with retry and circuit breaker
#        -> 502 if all providers fail (logged)
#        -> 200 on success — store to cache, log
#
# R-13 (logging on all exit paths): every branch calls log_request() before
# returning or raising. A log_kwargs dict is built up as the request
# progresses so partial context (team, tier, etc.) is captured even when
# the request fails early.

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session

from gateway.auth import InvalidApiKeyError, resolve_team
from gateway.cache import (
    exact_lookup,
    make_cache_key,
    semantic_lookup,
    store,
)
from gateway.config import get_pricing, get_routing_rules
from gateway.cost import compute_cost
from gateway.db import get_session
from gateway.embeddings import embed
from gateway.logging_service import log_request
from gateway.models import Team
from gateway.providers.circuit_breaker import get_breaker
from gateway.providers.gemini_client import GeminiClient
from gateway.providers.groq_client import GroqClient
from gateway.providers.openrouter_client import OpenRouterClient
from gateway.quota import QuotaExceededError, RateLimitedError, check_daily_budget, check_rpm
from gateway.router import AllProvidersFailedError, classify, route_request
from gateway.schemas import ChatRequest, ChatResponse, Message, UsageInfo

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Provider clients — built once at module load (keys come from env via config)
# ---------------------------------------------------------------------------

_provider_clients = {
    "groq": GroqClient(),
    "gemini": GeminiClient(),
    "openrouter": OpenRouterClient(),
}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/v1/chat/completions", response_model=ChatResponse)
async def chat_completions(
    body: ChatRequest,
    x_team_key: str = Header(default="", alias="X-Team-Key"),
    session: Session = Depends(get_session),
) -> ChatResponse:
    """Core gateway endpoint — OpenAI-compatible chat completions.

    Full pipeline: auth -> quota -> cache -> routing -> cost -> logging.
    Every exit path writes a RequestLog row (R-13).
    """
    t_start = time.monotonic()
    rules = get_routing_rules()
    pricing = get_pricing()

    # Shared log kwargs — built up as we learn more about the request.
    # Passed to log_request() on every exit path.
    log_kwargs: dict = dict(
        team_id=None,
        route_decision="",
        provider="",
        model="",
        cache_status="miss",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        latency_ms=0,
        success=False,
        error_message=None,
    )

    # Convert Pydantic Message objects to plain dicts for internal use
    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    # ------------------------------------------------------------------
    # Step 1: Auth
    # ------------------------------------------------------------------
    team: Optional[Team] = None
    try:
        team = resolve_team(session, x_team_key)
        log_kwargs["team_id"] = team.id
    except InvalidApiKeyError:
        log_kwargs["error_message"] = "invalid_api_key"
        log_kwargs["latency_ms"] = int((time.monotonic() - t_start) * 1000)
        log_request(session, **log_kwargs)
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    # ------------------------------------------------------------------
    # Step 2: RPM check
    # ------------------------------------------------------------------
    try:
        check_rpm(session, team)
    except RateLimitedError as exc:
        log_kwargs["error_message"] = "rate_limited"
        log_kwargs["latency_ms"] = int((time.monotonic() - t_start) * 1000)
        log_request(session, **log_kwargs)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )

    # ------------------------------------------------------------------
    # Step 3: Daily budget check
    # ------------------------------------------------------------------
    try:
        check_daily_budget(session, team)
    except QuotaExceededError as exc:
        log_kwargs["error_message"] = "quota_exceeded"
        log_kwargs["latency_ms"] = int((time.monotonic() - t_start) * 1000)
        log_request(session, **log_kwargs)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "reset_at": exc.reset_at.isoformat() + "Z",
            },
        )

    # ------------------------------------------------------------------
    # Step 4: Classify (needed for cache key and tier-scoped lookup)
    # ------------------------------------------------------------------
    tier = classify(messages, rules)
    log_kwargs["route_decision"] = tier

    # ------------------------------------------------------------------
    # Step 5: Exact cache lookup
    # ------------------------------------------------------------------
    cache_key = make_cache_key(tier, messages, body.temperature, body.max_tokens)
    exact_entry = exact_lookup(session, cache_key)
    if exact_entry is not None:
        cached_data = json.loads(exact_entry.response_json)
        log_kwargs.update(
            provider="cache",
            model=cached_data.get("model", ""),
            cache_status="exact",
            success=True,
            latency_ms=int((time.monotonic() - t_start) * 1000),
        )
        log_request(session, **log_kwargs)
        return _build_response(cached_data, log_kwargs)

    # ------------------------------------------------------------------
    # Step 6: Semantic cache lookup
    # ------------------------------------------------------------------
    last_user_text = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
    )
    if last_user_text:
        query_embedding = embed(last_user_text)
        semantic_entry = semantic_lookup(
            session, tier, query_embedding,
            threshold=rules.cache.semantic_similarity_threshold,
        )
        if semantic_entry is not None:
            cached_data = json.loads(semantic_entry.response_json)
            log_kwargs.update(
                provider="cache",
                model=cached_data.get("model", ""),
                cache_status="semantic",
                success=True,
                latency_ms=int((time.monotonic() - t_start) * 1000),
            )
            log_request(session, **log_kwargs)
            return _build_response(cached_data, log_kwargs)

    # ------------------------------------------------------------------
    # Step 7: Route to provider
    # ------------------------------------------------------------------
    try:
        response, provider_name, model_name = await route_request(
            messages=messages,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            rules=rules,
            provider_clients=_provider_clients,
        )
    except AllProvidersFailedError as exc:
        log_kwargs.update(
            error_message=str(exc),
            latency_ms=int((time.monotonic() - t_start) * 1000),
        )
        log_request(session, **log_kwargs)
        raise HTTPException(
            status_code=502,
            detail={"error": "all_providers_failed", "detail": str(exc)},
        )

    # ------------------------------------------------------------------
    # Step 8: Cost, cache store, log success
    # ------------------------------------------------------------------
    cost = compute_cost(
        provider_name, model_name,
        response.input_tokens, response.output_tokens,
        pricing,
    )

    # Persist to cache for future hits
    response_payload = {
        "text": response.text,
        "model": model_name,
        "provider": provider_name,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
    }
    store(
        session=session,
        key=cache_key,
        model_tier=tier,
        prompt_text=last_user_text,
        embedding=embed(last_user_text) if last_user_text else embed(""),
        response_json=json.dumps(response_payload),
        ttl_seconds=rules.cache.ttl_seconds,
    )

    latency_ms = int((time.monotonic() - t_start) * 1000)
    log_kwargs.update(
        provider=provider_name,
        model=model_name,
        cache_status="miss",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        success=True,
    )
    log_request(session, **log_kwargs)

    return ChatResponse(
        route_decision=tier,
        provider=provider_name,
        model=model_name,
        cache_status="miss",
        message=Message(role="assistant", content=response.text),
        usage=UsageInfo(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=cost,
        ),
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _build_response(cached_data: dict, log_kwargs: dict) -> ChatResponse:
    """Build a ChatResponse from a cached response payload dict."""
    return ChatResponse(
        route_decision=log_kwargs["route_decision"],
        provider="cache",
        model=cached_data.get("model", ""),
        cache_status=log_kwargs["cache_status"],
        message=Message(role="assistant", content=cached_data.get("text", "")),
        usage=UsageInfo(
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        ),
        latency_ms=log_kwargs["latency_ms"],
    )
