# gateway/quota.py
# Per-team token budget and requests-per-minute enforcement.
# Spec: docs/02_ARCHITECTURE.md §10, docs/04_BUILD_PLAN.md Phase 5
#
# Session injection rule: both functions accept session as a parameter.
# They never call get_session() internally.
#
# NULL team_id guard: all queries are parameterised by team.id so that
# RequestLog rows with team_id=None (auth failures) are never included
# in a team's counts.

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from gateway.models import RequestLog, Team

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class RateLimitedError(Exception):
    """Raised when a team has exceeded its requests-per-minute limit.

    Attributes:
        team_id: The team that was rate-limited.
        retry_after_seconds: Approximate seconds until the oldest request
            in the current window ages out and a slot opens up.
    """

    def __init__(self, team_id: str, retry_after_seconds: int) -> None:
        self.team_id = team_id
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Team {team_id!r} exceeded RPM limit. "
            f"Retry after {retry_after_seconds}s."
        )


class QuotaExceededError(Exception):
    """Raised when a team has exhausted its daily token budget.

    Attributes:
        team_id: The team whose budget is exhausted.
        reset_at: UTC datetime when the budget resets (next midnight UTC).
    """

    def __init__(self, team_id: str, reset_at: datetime) -> None:
        self.team_id = team_id
        self.reset_at = reset_at
        super().__init__(
            f"Team {team_id!r} exceeded daily token budget. "
            f"Resets at {reset_at.isoformat()}Z."
        )


# ---------------------------------------------------------------------------
# check_rpm
# ---------------------------------------------------------------------------

def check_rpm(session: Session, team: Team) -> None:
    """Raise RateLimitedError if the team has reached its RPM limit.

    Counts RequestLog rows for this team in the last 60 seconds.
    The window is a rolling 60-second lookback, not a fixed minute bucket.

    Args:
        session: Active SQLModel session (injected by the route handler).
        team: The resolved Team row for the current request.

    Raises:
        RateLimitedError: If the team's request count in the last 60s
            is >= team.rpm_limit.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window_start = now - timedelta(seconds=60)

    stmt = (
        select(func.count(RequestLog.id))
        .where(RequestLog.team_id == team.id)
        .where(RequestLog.timestamp >= window_start)
    )
    count: int = session.exec(stmt).one()

    logger.debug(
        "RPM check: team=%r count=%d limit=%d", team.id, count, team.rpm_limit
    )

    if count >= team.rpm_limit:
        # Approximate retry window: find oldest request in window, compute
        # how many seconds until it ages out of the 60s rolling window.
        oldest_stmt = (
            select(RequestLog.timestamp)
            .where(RequestLog.team_id == team.id)
            .where(RequestLog.timestamp >= window_start)
            .order_by(RequestLog.timestamp)
            .limit(1)
        )
        oldest_ts = session.exec(oldest_stmt).first()
        if oldest_ts is not None:
            retry_after = max(1, 60 - int((now - oldest_ts).total_seconds()))
        else:
            retry_after = 60

        logger.warning(
            "Rate limited: team=%r requests_in_window=%d limit=%d retry_after=%ds",
            team.id, count, team.rpm_limit, retry_after,
        )
        raise RateLimitedError(team_id=team.id, retry_after_seconds=retry_after)


# ---------------------------------------------------------------------------
# check_daily_budget
# ---------------------------------------------------------------------------

def check_daily_budget(session: Session, team: Team) -> None:
    """Raise QuotaExceededError if the team has exhausted its daily token budget.

    Sums input_tokens + output_tokens for this team since UTC midnight today.
    Only successful requests (success=True) consume budget — rejected and
    failed requests are logged but do not count against the budget.

    Args:
        session: Active SQLModel session (injected by the route handler).
        team: The resolved Team row for the current request.

    Raises:
        QuotaExceededError: If tokens used today >= team.daily_token_budget.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            func.coalesce(
                func.sum(RequestLog.input_tokens + RequestLog.output_tokens),
                0,
            )
        )
        .where(RequestLog.team_id == team.id)
        .where(RequestLog.timestamp >= midnight)
        .where(RequestLog.success == True)  # noqa: E712 — SQLModel requires == not is
    )
    tokens_used: int = session.exec(stmt).one()

    logger.debug(
        "Budget check: team=%r tokens_used=%d budget=%d",
        team.id, tokens_used, team.daily_token_budget,
    )

    if tokens_used >= team.daily_token_budget:
        # Reset is at the next UTC midnight
        reset_at = midnight + timedelta(days=1)
        logger.warning(
            "Quota exceeded: team=%r tokens_used=%d budget=%d reset_at=%s",
            team.id, tokens_used, team.daily_token_budget, reset_at.isoformat(),
        )
        raise QuotaExceededError(team_id=team.id, reset_at=reset_at)
