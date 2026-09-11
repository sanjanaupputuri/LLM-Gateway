# gateway/schemas.py
# Pydantic request/response models for the HTTP API.
# Spec: docs/03_API_CONTRACTS.md, docs/02_ARCHITECTURE.md §4

from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message] = Field(..., min_length=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=4096)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

class UsageInfo(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float


class ChatResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"req_{uuid.uuid4().hex[:8]}")
    route_decision: str           # "simple" | "complex"
    provider: str                 # "groq" | "gemini" | "openrouter" | "cache"
    model: str
    cache_status: str             # "miss" | "exact" | "semantic"
    message: Message
    usage: UsageInfo
    latency_ms: int


# ---------------------------------------------------------------------------
# Error bodies (used in HTTPException detail dicts)
# ---------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    error: str
    detail: Optional[str] = None
