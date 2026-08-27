# gateway/config.py
# Loads environment variables (.env) and YAML config files into validated
# Pydantic models. Single point of truth for all runtime configuration.
# Spec: docs/02_ARCHITECTURE.md §5, 04_BUILD_PLAN.md Phase 3

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Load .env into os.environ at import time (no-op if .env doesn't exist).
load_dotenv(override=False)


# ---------------------------------------------------------------------------
# Routing rules schema
# ---------------------------------------------------------------------------

class RouteStep(BaseModel):
    """A single provider/model entry in a chain."""
    provider: str   # "groq" | "gemini" | "openrouter"
    model: str


class RouteChain(BaseModel):
    chain: list[RouteStep]


class ClassifierConfig(BaseModel):
    max_simple_chars: int = 400
    complex_keywords: list[str] = Field(default_factory=list)


class CacheConfig(BaseModel):
    semantic_similarity_threshold: float = 0.92
    ttl_seconds: int = 86400


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 3
    cooldown_seconds: int = 60


class RoutesConfig(BaseModel):
    simple: RouteChain
    complex: RouteChain


class RoutingRules(BaseModel):
    classifier: ClassifierConfig
    routes: RoutesConfig
    cache: CacheConfig
    circuit_breaker: CircuitBreakerConfig


# ---------------------------------------------------------------------------
# Pricing schema
# ---------------------------------------------------------------------------

class ModelPricing(BaseModel):
    """Per-1M-token rates in USD."""
    input: float = 0.0
    output: float = 0.0


class PricingConfig(BaseModel):
    # Keys are "provider/model", e.g. "groq/llama-3.1-8b-instant"
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Top-level settings (env vars)
# ---------------------------------------------------------------------------

class Settings(BaseModel):
    groq_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")
    openrouter_api_key: str = Field(default="")
    gateway_db_path: str = Field(default="./data/gateway.db")
    log_level: str = Field(default="INFO")


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML file and return the parsed dict. Raises FileNotFoundError if missing."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p.resolve()}")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load environment variables into a Settings object. Cached after first call."""
    return Settings(
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        gateway_db_path=os.getenv("GATEWAY_DB_PATH", "./data/gateway.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


@lru_cache(maxsize=1)
def get_routing_rules(path: str = "config/routing_rules.yaml") -> RoutingRules:
    """Load and validate routing_rules.yaml. Cached after first call."""
    raw = _load_yaml(path)
    rules = RoutingRules(**raw)
    logger.info("Loaded routing rules from %s.", path)
    return rules


@lru_cache(maxsize=1)
def get_pricing(path: str = "config/pricing.yaml") -> PricingConfig:
    """Load and validate pricing.yaml. Cached after first call."""
    raw = _load_yaml(path)
    # Normalize: wrap each entry as ModelPricing if it's a plain dict.
    pricing_raw = raw.get("pricing", {})
    pricing = {k: ModelPricing(**v) for k, v in pricing_raw.items()}
    config = PricingConfig(pricing=pricing)
    logger.info("Loaded pricing config from %s (%d entries).", path, len(pricing))
    return config


def clear_config_cache() -> None:
    """Clear all lru_cache entries — useful in tests to reload config with different paths."""
    get_settings.cache_clear()
    get_routing_rules.cache_clear()
    get_pricing.cache_clear()
