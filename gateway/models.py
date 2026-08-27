# gateway/models.py
# SQLModel table definitions — Team, RequestLog, CacheEntry.
# Spec: docs/02_ARCHITECTURE.md §4
# Do NOT import from gateway.db here — models must be importable standalone
# (avoids circular imports when db.py imports models).

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel


class Team(SQLModel, table=True):
    """A caller identity with its own API key and usage budgets."""

    id: str = Field(primary_key=True)          # e.g. "team-sales"
    name: str
    api_key: str = Field(unique=True, index=True)
    daily_token_budget: int = Field(default=200_000)
    rpm_limit: int = Field(default=20)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RequestLog(SQLModel, table=True):
    """One row per request (cache hit or provider call, including 429s and errors)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
    team_id: Optional[str] = Field(default=None, index=True)  # None for auth failures
    route_decision: str = Field(default="")    # "simple" | "complex" | "" (auth failure)
    provider: str = Field(default="")          # "groq" | "gemini" | "openrouter" | "cache"
    model: str = Field(default="")
    cache_status: str = Field(default="miss")  # "miss" | "exact" | "semantic"
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cost_usd: float = Field(default=0.0)
    latency_ms: int = Field(default=0)
    success: bool = Field(default=False)
    error_message: Optional[str] = Field(default=None)


class CacheEntry(SQLModel, table=True):
    """Cached LLM response, keyed by exact hash and searchable by embedding."""

    # Suppress Pydantic's "model_" namespace warning for the model_tier field.
    model_config = ConfigDict(protected_namespaces=())

    id: Optional[int] = Field(default=None, primary_key=True)
    cache_key: str = Field(index=True)         # sha256 hash for exact match
    model_tier: str                             # "simple" | "complex"
    prompt_text: str                            # normalized latest user message
    embedding: bytes                            # np.ndarray.tobytes() — all-MiniLM-L6-v2
    response_json: str                          # full serialized response (JSON string)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    hit_count: int = Field(default=0)
