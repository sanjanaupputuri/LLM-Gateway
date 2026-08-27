# gateway/db.py
# SQLModel engine, session factory, init_db(), and team seeding.
# Spec: docs/02_ARCHITECTURE.md §4 + 04_BUILD_PLAN.md Phase 1

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Generator

import yaml
from sqlmodel import Session, SQLModel, create_engine

from gateway.models import Team

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine — built once at import time from GATEWAY_DB_PATH env var.
# Tests override this by monkeypatching `gateway.db.engine` or by using the
# `db_session` fixture in tests/conftest.py (which creates a fresh in-memory
# engine per test).
# ---------------------------------------------------------------------------

def _build_engine(db_url: str | None = None):
    """Create and return a SQLModel engine.

    Args:
        db_url: Full SQLAlchemy URL (e.g. 'sqlite:///./data/gateway.db').
                Falls back to GATEWAY_DB_PATH env var, then the default path.
    """
    if db_url is None:
        raw_path = os.getenv("GATEWAY_DB_PATH", "./data/gateway.db")
        # Ensure the parent directory exists before SQLite tries to create the file.
        db_path = Path(raw_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{db_path}"

    # check_same_thread=False is required for SQLite when used with FastAPI
    # (multiple threads share the same connection pool).
    connect_args = {"check_same_thread": False}
    return create_engine(db_url, connect_args=connect_args, echo=False)


engine = _build_engine()


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session per request."""
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# init_db — creates tables and seeds teams if empty
# ---------------------------------------------------------------------------

def init_db(
    db_engine=None,
    teams_yaml_path: str | Path = "config/teams.yaml",
) -> None:
    """Create all tables (if missing) and seed Team rows from teams.yaml.

    Idempotent: safe to call on every startup. Teams are only seeded when
    the Team table is completely empty (first run).

    Args:
        db_engine: Engine to use. Defaults to the module-level `engine`.
        teams_yaml_path: Path to the teams config YAML.
    """
    target_engine = db_engine or engine

    # Create all tables defined in SQLModel metadata.
    SQLModel.metadata.create_all(target_engine)
    logger.info("Database tables created / verified.")

    # Seed teams only when the table is empty.
    with Session(target_engine) as session:
        existing_count = len(session.exec(
            __import__("sqlmodel", fromlist=["select"]).select(Team)
        ).all())

        if existing_count > 0:
            logger.info("Team table already has %d rows — skipping seed.", existing_count)
            return

        yaml_path = Path(teams_yaml_path)
        if not yaml_path.exists():
            logger.warning(
                "teams.yaml not found at %s — no teams seeded.", yaml_path
            )
            return

        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        teams_data = data.get("teams", [])
        if not teams_data:
            logger.warning("teams.yaml has no entries under 'teams:' — no teams seeded.")
            return

        for entry in teams_data:
            team = Team(
                id=entry["id"],
                name=entry["name"],
                api_key=entry["api_key"],
                daily_token_budget=entry.get("daily_token_budget", 200_000),
                rpm_limit=entry.get("rpm_limit", 20),
            )
            session.add(team)

        session.commit()
        logger.info("Seeded %d teams from %s.", len(teams_data), yaml_path)
