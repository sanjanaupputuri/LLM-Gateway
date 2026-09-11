# gateway/main.py
# FastAPI application entrypoint.
# Spec: docs/02_ARCHITECTURE.md §1, docs/04_BUILD_PLAN.md Phase 6
#
# Routers mounted:
#   - chat.py     POST /v1/chat/completions  (Phase 5/6)
#   - admin.py    GET/POST /v1/admin/teams   (Phase 7)
#   - health.py   GET /healthz               (Phase 7)

from __future__ import annotations

import logging

from fastapi import FastAPI

from gateway.config import get_settings
from gateway.db import init_db
from gateway.routes.chat import router as chat_router

# Phase 7 stubs — imported once implemented
# from gateway.routes.admin import router as admin_router
# from gateway.routes.health import router as health_router

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="LLM Gateway",
    description="Routing, caching, and cost control for multiple LLM providers.",
    version="0.1.0",
)


@app.on_event("startup")
def on_startup() -> None:
    """Initialise the database and evict stale cache entries on startup."""
    logger.info("Gateway starting up — initialising database.")
    init_db()
    logger.info("Gateway ready.")


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(chat_router)

# Phase 7 — uncomment when admin.py and health.py are implemented:
# app.include_router(admin_router)
# app.include_router(health_router)
