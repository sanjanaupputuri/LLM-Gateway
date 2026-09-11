# gateway/logging_service.py
# Writes RequestLog rows to the database.
# Spec: docs/02_ARCHITECTURE.md §10, docs/04_BUILD_PLAN.md Phase 6
#
# Session injection rule: log_request() accepts session as a parameter.
# It never calls get_session() internally.
#
# Called on every exit path in chat.py (R-13):
#   - 200 success (provider call)
#   - 200 cache hit (exact or semantic)
#   - 401 auth failure
#   - 429 rate limited
#   - 429 quota exceeded
#   - 502 all providers failed
#   - 504 timeout

from __future__ import annotations

import logging

from sqlmodel import Session

from gateway.models import RequestLog

logger = logging.getLogger(__name__)


def log_request(
    session: Session,
    *,
    team_id: str | None,
    route_decision: str,
    provider: str,
    model: str,
    cache_status: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    success: bool,
    error_message: str | None = None,
) -> RequestLog:
    """Write a RequestLog row for a completed (or failed) request.

    All parameters are keyword-only to prevent positional argument confusion
    given the number of fields.

    Args:
        session: Active SQLModel session (injected by the route handler).
        team_id: The resolved team ID, or None for auth failures.
        route_decision: "simple" | "complex" | "" (empty for auth failures
            logged before classification runs).
        provider: "groq" | "gemini" | "openrouter" | "cache" | "" (auth failures).
        model: Model name, or "" for auth failures / cache hits.
        cache_status: "miss" | "exact" | "semantic".
        input_tokens: Prompt tokens consumed (0 for cache hits and errors).
        output_tokens: Completion tokens produced (0 for cache hits and errors).
        cost_usd: Computed cost in USD (0.0 for cache hits and errors).
        latency_ms: Wall-clock milliseconds from request receipt to response.
        success: True if a response was returned to the caller; False otherwise.
        error_message: Short error description for failed requests.

    Returns:
        The persisted RequestLog row (with id populated after commit).
    """
    entry = RequestLog(
        team_id=team_id,
        route_decision=route_decision,
        provider=provider,
        model=model,
        cache_status=cache_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        success=success,
        error_message=error_message,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)

    logger.debug(
        "Logged request: team=%r provider=%r model=%r cache=%r "
        "tokens=%d+%d cost=%.8f latency=%dms success=%s",
        team_id,
        provider,
        model,
        cache_status,
        input_tokens,
        output_tokens,
        cost_usd,
        latency_ms,
        success,
    )
    return entry
