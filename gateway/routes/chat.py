# gateway/routes/chat.py
# POST /v1/chat/completions — core gateway endpoint.
# Spec: docs/03_API_CONTRACTS.md, docs/02_ARCHITECTURE.md §10
#
# Phase 5 scope: auth + quota checks wired and returning correct HTTP errors.
# Phase 6 will add: cache lookup, route_request(), cost computation, logging.
#
# Order of operations (per §10):
#   1. Resolve team from X-Team-Key header (401 if unknown)
#   2. Check RPM (429 rate_limited if over limit)
#   3. Check daily token budget (429 quota_exceeded if exhausted)
#   4. [Phase 6] Cache lookup, then routing, then logging
#
# Logging note (R-13): log_request() must be called on every exit path.
# This is implemented in Phase 6 using a try/finally pattern. The stub
# below does not yet log — that gap is intentional and tracked in RISKS.md.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session

from gateway.auth import InvalidApiKeyError, resolve_team
from gateway.db import get_session
from gateway.quota import QuotaExceededError, RateLimitedError, check_daily_budget, check_rpm

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: dict,
    x_team_key: str = Header(default="", alias="X-Team-Key"),
    session: Session = Depends(get_session),
):
    """Core gateway endpoint — OpenAI-compatible chat completions.

    Phase 5: auth and quota checks only.
    Phase 6: cache, routing, cost attribution, and logging.
    """
    # --- Step 1: Auth ---
    try:
        team = resolve_team(session, x_team_key)
    except InvalidApiKeyError:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    # --- Step 2: RPM check ---
    try:
        check_rpm(session, team)
    except RateLimitedError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )

    # --- Step 3: Daily budget check ---
    try:
        check_daily_budget(session, team)
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "reset_at": exc.reset_at.isoformat() + "Z",
            },
        )

    # --- Steps 4+: Cache lookup, routing, cost, logging (Phase 6) ---
    # Placeholder response so the endpoint is reachable and quota tests pass.
    # This will be replaced in Phase 6 with the full pipeline.
    logger.debug("Auth and quota checks passed for team=%r — routing (Phase 6)", team.id)
    return {"message": "Phase 6 not yet implemented", "team_id": team.id}
