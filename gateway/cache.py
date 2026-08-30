# gateway/cache.py
# Two-level cache: exact (SHA-256 hash) + semantic (cosine similarity).
# Spec: docs/02_ARCHITECTURE.md §9, docs/04_BUILD_PLAN.md Phase 4

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Literal

import numpy as np
from sqlmodel import Session, select

from gateway.models import CacheEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# make_cache_key
# ---------------------------------------------------------------------------

def make_cache_key(
    model_tier: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> str:
    """Compute a deterministic SHA-256 cache key for an exact-match lookup.

    The key encodes all parameters that affect the response, so two
    requests that differ in *any* way produce different keys.

    Args:
        model_tier: "simple" | "complex"
        messages: OpenAI-style message list.
        temperature: Sampling temperature.
        max_tokens: Max tokens requested.

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    payload = {
        "model_tier": model_tier,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# exact_lookup
# ---------------------------------------------------------------------------

def exact_lookup(session: Session, key: str) -> CacheEntry | None:
    """Look up a non-expired CacheEntry by its exact SHA-256 key.

    Args:
        session: Active SQLModel session.
        key: SHA-256 hex digest produced by make_cache_key().

    Returns:
        The matching CacheEntry if found and not expired, else None.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC to match DB
    stmt = (
        select(CacheEntry)
        .where(CacheEntry.cache_key == key)
        .where(CacheEntry.expires_at > now)
    )
    entry = session.exec(stmt).first()
    if entry is not None:
        entry.hit_count += 1
        session.add(entry)
        session.commit()
        logger.debug("Exact cache hit: key=%s hit_count=%d", key[:12], entry.hit_count)
    return entry


# ---------------------------------------------------------------------------
# semantic_lookup
# ---------------------------------------------------------------------------

def semantic_lookup(
    session: Session,
    model_tier: str,
    query_embedding: np.ndarray,
    threshold: float,
) -> CacheEntry | None:
    """Find the most similar non-expired CacheEntry for the given tier.

    Brute-force cosine similarity over all non-expired rows for model_tier.
    Since both stored and query embeddings are L2-normalised, cosine
    similarity == dot product.

    Args:
        session: Active SQLModel session.
        model_tier: "simple" | "complex" — only entries of this tier are
            considered (cache is tier-scoped per spec §9).
        query_embedding: L2-normalised float32 array (shape 384).
        threshold: Minimum cosine similarity to count as a hit.

    Returns:
        The CacheEntry with the highest similarity if ≥ threshold, else None.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = (
        select(CacheEntry)
        .where(CacheEntry.model_tier == model_tier)
        .where(CacheEntry.expires_at > now)
    )
    entries: list[CacheEntry] = session.exec(stmt).all()

    if not entries:
        return None

    best_entry: CacheEntry | None = None
    best_sim: float = -1.0

    for entry in entries:
        stored = np.frombuffer(entry.embedding, dtype=np.float32)
        sim = float(np.dot(query_embedding, stored))
        if sim > best_sim:
            best_sim = sim
            best_entry = entry

    if best_sim >= threshold:
        assert best_entry is not None
        best_entry.hit_count += 1
        session.add(best_entry)
        session.commit()
        logger.debug(
            "Semantic cache hit: similarity=%.4f threshold=%.4f hit_count=%d",
            best_sim,
            threshold,
            best_entry.hit_count,
        )
        return best_entry

    logger.debug(
        "Semantic cache miss: best_similarity=%.4f threshold=%.4f",
        best_sim,
        threshold,
    )
    return None


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def store(
    session: Session,
    key: str,
    model_tier: str,
    prompt_text: str,
    embedding: np.ndarray,
    response_json: str,
    ttl_seconds: int,
) -> CacheEntry:
    """Persist a new CacheEntry to the database.

    Args:
        session: Active SQLModel session.
        key: SHA-256 hex digest (from make_cache_key).
        model_tier: "simple" | "complex".
        prompt_text: The normalised last-user-message text (used for
            semantic lookup comparisons and dashboard display).
        embedding: L2-normalised float32 ndarray from embed().
        response_json: Full serialised provider response (JSON string).
        ttl_seconds: Seconds until expiry from now.

    Returns:
        The newly created CacheEntry.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_at = datetime(
        now.year, now.month, now.day,
        now.hour, now.minute, now.second,
    )
    # Add TTL using timedelta
    from datetime import timedelta
    expires_at = now + timedelta(seconds=ttl_seconds)

    entry = CacheEntry(
        cache_key=key,
        model_tier=model_tier,
        prompt_text=prompt_text,
        embedding=embedding.astype(np.float32).tobytes(),
        response_json=response_json,
        created_at=now,
        expires_at=expires_at,
        hit_count=0,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    logger.debug(
        "Stored cache entry: key=%s tier=%s ttl=%ds expires_at=%s",
        key[:12],
        model_tier,
        ttl_seconds,
        expires_at.isoformat(),
    )
    return entry
