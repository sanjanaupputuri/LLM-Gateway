# gateway/auth.py
# Resolves X-Team-Key header to a Team row.
# Spec: docs/02_ARCHITECTURE.md §10, docs/04_BUILD_PLAN.md Phase 5
#
# Session injection rule: resolve_team() accepts session as a parameter.
# It never calls get_session() internally — only the route handler (chat.py)
# depends on get_session via FastAPI's Depends().

from __future__ import annotations

import logging

from sqlmodel import Session, select

from gateway.models import Team

logger = logging.getLogger(__name__)


class InvalidApiKeyError(Exception):
    """Raised when the X-Team-Key header is missing or does not match any Team."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        super().__init__(f"Invalid or unknown API key: {api_key!r}")


def resolve_team(session: Session, api_key: str) -> Team:
    """Look up the Team row for the given API key.

    Args:
        session: Active SQLModel session (injected by the route handler).
        api_key: Value of the X-Team-Key request header.

    Returns:
        The matching Team row.

    Raises:
        InvalidApiKeyError: If no Team with that api_key exists.
    """
    if not api_key:
        raise InvalidApiKeyError(api_key)

    stmt = select(Team).where(Team.api_key == api_key)
    team = session.exec(stmt).first()

    if team is None:
        logger.warning("Auth failure: unknown api_key=%r", api_key[:8] + "...")
        raise InvalidApiKeyError(api_key)

    logger.debug("Auth success: team_id=%r", team.id)
    return team
