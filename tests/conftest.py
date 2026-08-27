# tests/conftest.py
# Shared pytest fixtures for the LLM Gateway test suite.
# All tests use an in-memory SQLite DB — never touches data/gateway.db.

from __future__ import annotations

from pathlib import Path
from typing import Generator

import pytest
from sqlmodel import Session, SQLModel, create_engine

from gateway.config import clear_config_cache
from gateway.db import get_session, init_db
from gateway.models import Team  # noqa: F401 — imported so metadata is populated


# ---------------------------------------------------------------------------
# In-memory engine fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def db_engine():
    """Create a fresh in-memory SQLite engine per test function.

    Using scope="function" ensures complete isolation — each test starts
    with an empty database and tables are dropped at the end of the test.
    """
    engine = create_engine(
        "sqlite://",  # pure in-memory, no file created
        connect_args={"check_same_thread": False},
        echo=False,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


# ---------------------------------------------------------------------------
# Session fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def db_session(db_engine) -> Generator[Session, None, None]:
    """Yield a Session bound to the in-memory engine.

    All writes within a test use this session. The engine is torn down after
    each test so there is no state leakage between tests.
    """
    with Session(db_engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Seeded-DB fixture (engine + session + 3 seeded teams)
# ---------------------------------------------------------------------------

TEAMS_YAML_PATH = Path(__file__).parent.parent / "config" / "teams.yaml"


@pytest.fixture(scope="function")
def seeded_db_engine(db_engine):
    """In-memory engine pre-seeded with the 3 demo teams from config/teams.yaml."""
    init_db(db_engine=db_engine, teams_yaml_path=str(TEAMS_YAML_PATH))
    return db_engine


@pytest.fixture(scope="function")
def seeded_db_session(seeded_db_engine) -> Generator[Session, None, None]:
    """Session bound to a seeded in-memory engine — teams already present."""
    with Session(seeded_db_engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Config cache isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_caches():
    """Clear lru_cache config loaders before every test to prevent cache pollution."""
    clear_config_cache()
    yield
    clear_config_cache()


# ---------------------------------------------------------------------------
# FastAPI test client fixture (used from Phase 6 onwards)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def app_client(seeded_db_engine):
    """httpx AsyncClient pointed at the FastAPI app with the in-memory DB injected.

    The app is imported lazily so this fixture only fails if gateway/main.py
    is non-importable — it won't fail in Phase 1 because the import is guarded.
    """
    try:
        from httpx import AsyncClient
        from fastapi.testclient import TestClient
        from gateway.main import app
        from gateway.db import get_session as real_get_session

        def override_get_session():
            with Session(seeded_db_engine) as session:
                yield session

        app.dependency_overrides[real_get_session] = override_get_session
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()
    except ImportError:
        pytest.skip("gateway.main not yet implemented — skipping app_client fixture")
