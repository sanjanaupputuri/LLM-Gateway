# tests/test_cache.py
# Cache tests: CA-01 through CA-05.
# Spec: docs/05_TEST_PLAN.md §3.3, docs/04_BUILD_PLAN.md Phase 4
#
# Uses a real in-memory SQLite DB (via db_session fixture from conftest.py)
# and the real all-MiniLM-L6-v2 model (loaded once per session via
# gateway.embeddings) so cosine similarity is genuine — no mocking of the
# embedding math.

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from gateway.cache import exact_lookup, make_cache_key, semantic_lookup, store
from gateway.embeddings import embed
from gateway.models import CacheEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TIER_SIMPLE = "simple"
TIER_COMPLEX = "complex"
THRESHOLD = 0.92  # matches routing_rules.yaml default

def _fake_response(text: str = "cached answer") -> str:
    return json.dumps({"choices": [{"message": {"content": text}}]})


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _store_entry(
    session,
    prompt: str,
    tier: str = TIER_SIMPLE,
    ttl_seconds: int = 86400,
    response_text: str = "cached answer",
) -> CacheEntry:
    """Helper: embed prompt, make a key, and store a CacheEntry."""
    emb = embed(prompt)
    key = make_cache_key(tier, [{"role": "user", "content": prompt}], 0.7, 64)
    return store(
        session=session,
        key=key,
        model_tier=tier,
        prompt_text=prompt,
        embedding=emb,
        response_json=_fake_response(response_text),
        ttl_seconds=ttl_seconds,
    )


# ---------------------------------------------------------------------------
# CA-01: Exact cache — miss on first call, hit on second identical call
# ---------------------------------------------------------------------------

def test_ca01_exact_miss_then_hit(db_session):
    """First lookup returns None (miss); after store(), same key returns the entry."""
    messages = [{"role": "user", "content": "What is the boiling point of water?"}]
    key = make_cache_key(TIER_SIMPLE, messages, 0.7, 64)

    # First lookup — must miss
    result = exact_lookup(db_session, key)
    assert result is None, "Expected cache miss on first lookup"

    # Store the entry
    emb = embed(messages[0]["content"])
    entry = store(
        session=db_session,
        key=key,
        model_tier=TIER_SIMPLE,
        prompt_text=messages[0]["content"],
        embedding=emb,
        response_json=_fake_response("100°C"),
        ttl_seconds=86400,
    )
    assert entry.id is not None

    # Second lookup — must hit
    result2 = exact_lookup(db_session, key)
    assert result2 is not None, "Expected cache hit on second lookup"
    assert result2.cache_key == key
    assert result2.hit_count == 1


def test_ca01_hit_increments_hit_count(db_session):
    """Each exact hit increments hit_count by 1."""
    messages = [{"role": "user", "content": "How far is the Moon?"}]
    key = make_cache_key(TIER_SIMPLE, messages, 0.7, 64)
    emb = embed(messages[0]["content"])

    store(
        session=db_session,
        key=key,
        model_tier=TIER_SIMPLE,
        prompt_text=messages[0]["content"],
        embedding=emb,
        response_json=_fake_response(),
        ttl_seconds=86400,
    )

    exact_lookup(db_session, key)
    exact_lookup(db_session, key)
    result = exact_lookup(db_session, key)
    assert result is not None
    assert result.hit_count == 3


# ---------------------------------------------------------------------------
# CA-02: Semantic cache hit — paraphrased near-duplicate
# ---------------------------------------------------------------------------

def test_ca02_semantic_hit_on_paraphrase(db_session):
    """A paraphrase of the stored prompt exceeds the similarity threshold.

    Pair chosen because all-MiniLM-L6-v2 scores them ~0.99 — well above 0.92.
    """
    original = "How do I reset my password?"
    paraphrase = "How can I reset my password?"

    # Store the original
    orig_emb = embed(original)
    key = make_cache_key(TIER_SIMPLE, [{"role": "user", "content": original}], 0.7, 64)
    store(
        session=db_session,
        key=key,
        model_tier=TIER_SIMPLE,
        prompt_text=original,
        embedding=orig_emb,
        response_json=_fake_response("Q3 was great"),
        ttl_seconds=86400,
    )

    # Semantic lookup with the paraphrase
    query_emb = embed(paraphrase)
    result = semantic_lookup(db_session, TIER_SIMPLE, query_emb, THRESHOLD)

    assert result is not None, (
        f"Expected semantic hit for paraphrase {paraphrase!r} "
        f"(threshold={THRESHOLD})"
    )
    assert result.prompt_text == original
    assert result.hit_count == 1


# ---------------------------------------------------------------------------
# CA-03: Below similarity threshold — unrelated prompts do not match
# ---------------------------------------------------------------------------

def test_ca03_no_false_positive_for_unrelated_prompts(db_session):
    """Two clearly unrelated prompts in the same tier should NOT produce a semantic hit."""
    stored_prompt = "What is the capital of France?"
    unrelated_prompt = "Write me a Python function to sort a list"

    orig_emb = embed(stored_prompt)
    key = make_cache_key(
        TIER_SIMPLE, [{"role": "user", "content": stored_prompt}], 0.7, 64
    )
    store(
        session=db_session,
        key=key,
        model_tier=TIER_SIMPLE,
        prompt_text=stored_prompt,
        embedding=orig_emb,
        response_json=_fake_response("Paris"),
        ttl_seconds=86400,
    )

    query_emb = embed(unrelated_prompt)
    result = semantic_lookup(db_session, TIER_SIMPLE, query_emb, THRESHOLD)

    assert result is None, (
        f"Expected no semantic hit for unrelated prompt {unrelated_prompt!r} "
        f"(threshold={THRESHOLD})"
    )


# ---------------------------------------------------------------------------
# CA-04: TTL expiry — expired entries are not returned
# ---------------------------------------------------------------------------

def test_ca04_expired_entry_not_returned_exact(db_session):
    """An expired CacheEntry must not be returned by exact_lookup."""
    messages = [{"role": "user", "content": "Tell me a joke"}]
    key = make_cache_key(TIER_SIMPLE, messages, 0.7, 64)
    emb = embed(messages[0]["content"])

    # Store with -1s TTL so it expires immediately
    store(
        session=db_session,
        key=key,
        model_tier=TIER_SIMPLE,
        prompt_text=messages[0]["content"],
        embedding=emb,
        response_json=_fake_response("Why did the chicken..."),
        ttl_seconds=-1,  # already expired
    )

    result = exact_lookup(db_session, key)
    assert result is None, "Expired entry must not be returned by exact_lookup"


def test_ca04_expired_entry_not_returned_semantic(db_session):
    """An expired CacheEntry must not be returned by semantic_lookup."""
    prompt = "Tell me a joke about programmers"
    emb = embed(prompt)
    key = make_cache_key(TIER_SIMPLE, [{"role": "user", "content": prompt}], 0.7, 64)

    store(
        session=db_session,
        key=key,
        model_tier=TIER_SIMPLE,
        prompt_text=prompt,
        embedding=emb,
        response_json=_fake_response(),
        ttl_seconds=-1,  # already expired
    )

    # Query with the exact same embedding — would be a 1.0 similarity hit
    # if not expired
    result = semantic_lookup(db_session, TIER_SIMPLE, emb, threshold=0.0)
    assert result is None, "Expired entry must not be returned by semantic_lookup"


def test_ca04_manual_expired_entry(db_session):
    """Directly insert a CacheEntry with expires_at in the past."""
    now = _now_naive()
    past = now - timedelta(hours=2)

    entry = CacheEntry(
        cache_key="expired-key-abc123",
        model_tier=TIER_SIMPLE,
        prompt_text="expired prompt",
        embedding=embed("expired prompt").tobytes(),
        response_json=_fake_response("old response"),
        created_at=past,
        expires_at=past,  # in the past
        hit_count=0,
    )
    db_session.add(entry)
    db_session.commit()

    result = exact_lookup(db_session, "expired-key-abc123")
    assert result is None


# ---------------------------------------------------------------------------
# CA-05: Cache is tier-scoped — no cross-tier hits
# ---------------------------------------------------------------------------

def test_ca05_no_cross_tier_semantic_hit(db_session):
    """Same prompt stored under 'simple' tier must NOT hit when querying 'complex' tier."""
    prompt = "Explain how transformers work"
    emb = embed(prompt)
    key = make_cache_key(TIER_SIMPLE, [{"role": "user", "content": prompt}], 0.7, 64)

    # Store under SIMPLE tier
    store(
        session=db_session,
        key=key,
        model_tier=TIER_SIMPLE,
        prompt_text=prompt,
        embedding=emb,
        response_json=_fake_response("Transformers use attention"),
        ttl_seconds=86400,
    )

    # Semantic lookup against COMPLEX tier — must miss
    result = semantic_lookup(db_session, TIER_COMPLEX, emb, threshold=0.0)
    assert result is None, (
        "Cache entry stored under 'simple' must not be returned for 'complex' tier query"
    )


def test_ca05_no_cross_tier_exact_hit(db_session):
    """make_cache_key encodes tier, so exact keys differ between tiers for the same prompt."""
    messages = [{"role": "user", "content": "What is recursion?"}]
    key_simple = make_cache_key(TIER_SIMPLE, messages, 0.7, 64)
    key_complex = make_cache_key(TIER_COMPLEX, messages, 0.7, 64)

    assert key_simple != key_complex, (
        "Cache keys must differ between tiers even for identical messages"
    )

    # Store under simple
    emb = embed(messages[0]["content"])
    store(
        session=db_session,
        key=key_simple,
        model_tier=TIER_SIMPLE,
        prompt_text=messages[0]["content"],
        embedding=emb,
        response_json=_fake_response("Recursion is..."),
        ttl_seconds=86400,
    )

    # Exact lookup under complex key — must miss
    result = exact_lookup(db_session, key_complex)
    assert result is None, "Simple-tier entry must not be found by complex-tier key"


def test_ca05_each_tier_caches_independently(db_session):
    """Storing the same prompt in both tiers produces two independent entries."""
    prompt = "What is machine learning?"
    messages = [{"role": "user", "content": prompt}]
    emb = embed(prompt)

    key_simple = make_cache_key(TIER_SIMPLE, messages, 0.7, 64)
    key_complex = make_cache_key(TIER_COMPLEX, messages, 0.7, 64)

    store(db_session, key_simple, TIER_SIMPLE, prompt, emb, _fake_response("simple answer"), 86400)
    store(db_session, key_complex, TIER_COMPLEX, prompt, emb, _fake_response("complex answer"), 86400)

    hit_simple = exact_lookup(db_session, key_simple)
    hit_complex = exact_lookup(db_session, key_complex)

    assert hit_simple is not None
    assert hit_complex is not None
    assert hit_simple.id != hit_complex.id
    assert json.loads(hit_simple.response_json)["choices"][0]["message"]["content"] == "simple answer"
    assert json.loads(hit_complex.response_json)["choices"][0]["message"]["content"] == "complex answer"


# ---------------------------------------------------------------------------
# make_cache_key: determinism and sensitivity
# ---------------------------------------------------------------------------

def test_make_cache_key_is_deterministic():
    """Same inputs always produce the same key."""
    messages = [{"role": "user", "content": "Hello"}]
    k1 = make_cache_key("simple", messages, 0.7, 64)
    k2 = make_cache_key("simple", messages, 0.7, 64)
    assert k1 == k2


def test_make_cache_key_differs_on_temperature():
    """Different temperatures produce different keys."""
    messages = [{"role": "user", "content": "Hello"}]
    k1 = make_cache_key("simple", messages, 0.7, 64)
    k2 = make_cache_key("simple", messages, 0.5, 64)
    assert k1 != k2


def test_make_cache_key_differs_on_max_tokens():
    """Different max_tokens produce different keys."""
    messages = [{"role": "user", "content": "Hello"}]
    k1 = make_cache_key("simple", messages, 0.7, 64)
    k2 = make_cache_key("simple", messages, 0.7, 128)
    assert k1 != k2


def test_make_cache_key_is_64_hex_chars():
    """Key must be a 64-character lowercase hex string (SHA-256)."""
    key = make_cache_key("simple", [{"role": "user", "content": "test"}], 0.7, 64)
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)
