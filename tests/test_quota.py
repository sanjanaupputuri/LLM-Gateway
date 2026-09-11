# tests/test_quota.py
# Quota and rate limiting tests: QT-01 through QT-07.
# Spec: docs/05_TEST_PLAN.md, docs/04_BUILD_PLAN.md Phase 5
#
# Tests exercise auth.py and quota.py directly via their function interfaces
# (not via HTTP) since gateway/main.py is implemented in Phase 6.
# All tests use the seeded_db_session fixture (3 demo teams, in-memory SQLite).

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from gateway.auth import InvalidApiKeyError, resolve_team
from gateway.models import RequestLog, Team
from gateway.quota import (
    QuotaExceededError,
    RateLimitedError,
    check_daily_budget,
    check_rpm,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_log(
    session: Session,
    team_id: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    success: bool = True,
    seconds_ago: float = 0.0,
) -> RequestLog:
    """Insert a RequestLog row for a team, optionally back-dated."""
    ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds_ago)
    log = RequestLog(
        timestamp=ts,
        team_id=team_id,
        route_decision="simple",
        provider="groq",
        model="llama-3.1-8b-instant",
        cache_status="miss",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=0.0,
        latency_ms=100,
        success=success,
    )
    session.add(log)
    session.commit()
    return log


def _make_team(
    session: Session,
    *,
    team_id: str = "team-test",
    daily_token_budget: int = 1000,
    rpm_limit: int = 5,
) -> Team:
    """Insert a custom Team row for tests that need specific limits."""
    team = Team(
        id=team_id,
        name="Test Team",
        api_key=f"sk-test-{team_id}",
        daily_token_budget=daily_token_budget,
        rpm_limit=rpm_limit,
    )
    session.add(team)
    session.commit()
    return team


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

def test_resolve_team_valid_key(seeded_db_session):
    """resolve_team returns the correct Team for a known API key."""
    team = resolve_team(seeded_db_session, "sk-team-sales-demo-key")
    assert team.id == "team-sales"
    assert team.name == "Sales Team"


def test_resolve_team_invalid_key(seeded_db_session):
    """resolve_team raises InvalidApiKeyError for an unknown key."""
    with pytest.raises(InvalidApiKeyError):
        resolve_team(seeded_db_session, "sk-does-not-exist")


def test_resolve_team_empty_key(seeded_db_session):
    """resolve_team raises InvalidApiKeyError for an empty string."""
    with pytest.raises(InvalidApiKeyError):
        resolve_team(seeded_db_session, "")


def test_resolve_team_all_seeded_teams(seeded_db_session):
    """All 3 seeded teams resolve correctly."""
    keys = {
        "sk-team-sales-demo-key": "team-sales",
        "sk-team-support-demo-key": "team-support",
        "sk-team-eng-demo-key": "team-eng",
    }
    for key, expected_id in keys.items():
        team = resolve_team(seeded_db_session, key)
        assert team.id == expected_id


# ---------------------------------------------------------------------------
# QT-01: RPM — under limit passes silently
# ---------------------------------------------------------------------------

def test_qt01_rpm_under_limit_passes(db_session):
    """check_rpm does not raise when request count is below rpm_limit."""
    team = _make_team(db_session, rpm_limit=5)
    # Insert 4 recent requests (under the limit of 5)
    for i in range(4):
        _make_log(db_session, team.id, seconds_ago=i * 5)

    # Should not raise
    check_rpm(db_session, team)


# ---------------------------------------------------------------------------
# QT-02: RPM — exactly at limit raises RateLimitedError
# ---------------------------------------------------------------------------

def test_qt02_rpm_at_limit_raises(db_session):
    """check_rpm raises RateLimitedError when request count == rpm_limit."""
    team = _make_team(db_session, rpm_limit=5)
    for i in range(5):
        _make_log(db_session, team.id, seconds_ago=i * 5)

    with pytest.raises(RateLimitedError) as exc_info:
        check_rpm(db_session, team)

    assert exc_info.value.team_id == team.id
    assert exc_info.value.retry_after_seconds > 0


# ---------------------------------------------------------------------------
# QT-03: RPM — old requests (outside 60s window) do not count
# ---------------------------------------------------------------------------

def test_qt03_rpm_old_requests_ignored(db_session):
    """Requests older than 60 seconds do not count toward the RPM window."""
    team = _make_team(db_session, rpm_limit=3)
    # Insert 3 requests well outside the window
    for i in range(3):
        _make_log(db_session, team.id, seconds_ago=61 + i)
    # Insert 2 requests inside the window (under limit)
    for i in range(2):
        _make_log(db_session, team.id, seconds_ago=i * 5)

    # Only 2 in-window requests — should not raise
    check_rpm(db_session, team)


# ---------------------------------------------------------------------------
# QT-04: RPM — acceptance criteria: 21st request from team with rpm_limit=20
# ---------------------------------------------------------------------------

def test_qt04_21st_request_rate_limited(db_session):
    """21 rapid requests from a team with rpm_limit=20 — 21st is rate limited.

    This is the acceptance criterion from docs/04_BUILD_PLAN.md Phase 5.
    """
    team = _make_team(db_session, rpm_limit=20)

    # Simulate 20 successful recent requests (within the window)
    for i in range(20):
        _make_log(db_session, team.id, seconds_ago=i * 2)  # spread within 60s

    # 21st check must raise
    with pytest.raises(RateLimitedError) as exc_info:
        check_rpm(db_session, team)

    assert exc_info.value.team_id == team.id


# ---------------------------------------------------------------------------
# QT-05: Budget — under budget passes silently
# ---------------------------------------------------------------------------

def test_qt05_budget_under_limit_passes(db_session):
    """check_daily_budget does not raise when tokens used < daily_token_budget."""
    team = _make_team(db_session, daily_token_budget=1000)
    # 3 successful requests, 100+50=150 tokens each → 450 total (under 1000)
    for _ in range(3):
        _make_log(db_session, team.id, input_tokens=100, output_tokens=50, success=True)

    check_daily_budget(db_session, team)


# ---------------------------------------------------------------------------
# QT-06: Budget — at/over budget raises QuotaExceededError
# ---------------------------------------------------------------------------

def test_qt06_budget_exceeded_raises(db_session):
    """check_daily_budget raises QuotaExceededError when tokens used >= budget."""
    team = _make_team(db_session, daily_token_budget=500)
    # 4 requests of 100+50=150 tokens each → 600 tokens (over 500)
    for _ in range(4):
        _make_log(db_session, team.id, input_tokens=100, output_tokens=50, success=True)

    with pytest.raises(QuotaExceededError) as exc_info:
        check_daily_budget(db_session, team)

    assert exc_info.value.team_id == team.id
    # reset_at must be tomorrow's UTC midnight
    assert exc_info.value.reset_at > datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# QT-07: Budget — acceptance criteria: tiny budget, first OK then 429
# ---------------------------------------------------------------------------

def test_qt07_first_request_ok_second_rejected(db_session):
    """Team with tiny budget: first request passes, second is quota_exceeded.

    Mirrors the acceptance criterion from docs/04_BUILD_PLAN.md Phase 5:
    'gets a normal 200 on request 1, then 429 quota_exceeded on a subsequent
    request that would exceed budget — no provider call is made for the
    rejected request.'
    """
    # Budget of 100 tokens total
    team = _make_team(db_session, daily_token_budget=100)

    # First check — budget is 0, passes
    check_daily_budget(db_session, team)

    # Simulate first request completing successfully (80 tokens used)
    _make_log(db_session, team.id, input_tokens=50, output_tokens=30, success=True)

    # Budget now at 80/100 — still passes
    check_daily_budget(db_session, team)

    # Simulate second request completing (30 more tokens → total 110, over budget)
    _make_log(db_session, team.id, input_tokens=20, output_tokens=10, success=True)

    # Third check must raise
    with pytest.raises(QuotaExceededError):
        check_daily_budget(db_session, team)


# ---------------------------------------------------------------------------
# QT-08: Budget — failed requests do not consume budget
# ---------------------------------------------------------------------------

def test_qt08_failed_requests_do_not_consume_budget(db_session):
    """Requests with success=False must not count toward the daily token budget."""
    team = _make_team(db_session, daily_token_budget=100)

    # Insert many failed requests (would exceed budget if counted)
    for _ in range(10):
        _make_log(db_session, team.id, input_tokens=50, output_tokens=50, success=False)

    # Budget check must pass — failed requests don't consume budget
    check_daily_budget(db_session, team)


# ---------------------------------------------------------------------------
# QT-09: Budget — requests from other teams do not affect this team
# ---------------------------------------------------------------------------

def test_qt09_other_team_tokens_not_counted(db_session):
    """Token usage from other teams must not affect a team's budget check."""
    team_a = _make_team(db_session, team_id="team-a", daily_token_budget=100)
    team_b = _make_team(db_session, team_id="team-b", daily_token_budget=100)

    # Team B burns 90 tokens
    _make_log(db_session, team_b.id, input_tokens=60, output_tokens=30, success=True)

    # Team A's budget check must be unaffected
    check_daily_budget(db_session, team_a)


# ---------------------------------------------------------------------------
# QT-10: Budget — NULL team_id rows (auth failures) do not affect budget
# ---------------------------------------------------------------------------

def test_qt10_null_team_id_rows_not_counted(db_session):
    """RequestLog rows with team_id=None must not be counted in any team's budget."""
    team = _make_team(db_session, daily_token_budget=100)

    # Insert a log row with team_id=None (simulates an auth failure log)
    auth_fail_log = RequestLog(
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
        team_id=None,
        route_decision="",
        provider="",
        model="",
        cache_status="miss",
        input_tokens=999,
        output_tokens=999,
        cost_usd=0.0,
        latency_ms=0,
        success=False,
        error_message="invalid_api_key",
    )
    db_session.add(auth_fail_log)
    db_session.commit()

    # Team's budget must be unaffected by the NULL-team row
    check_daily_budget(db_session, team)
