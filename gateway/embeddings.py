# gateway/embeddings.py
# Loads all-MiniLM-L6-v2 once at module import and exposes embed().
# The model is loaded ONCE — not per-request — to avoid the ~300ms
# cold-start penalty on every cache lookup.
# Spec: docs/02_ARCHITECTURE.md §9, docs/04_BUILD_PLAN.md Phase 4

from __future__ import annotations

import logging

import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model — loaded once at import time.
# sentence-transformers downloads and caches the weights (~80 MB) on first
# use; subsequent imports load from the local HuggingFace cache instantly.
# ---------------------------------------------------------------------------

_MODEL_NAME = "all-MiniLM-L6-v2"

logger.info("Loading embedding model %r …", _MODEL_NAME)
_model: SentenceTransformer = SentenceTransformer(_MODEL_NAME)
logger.info("Embedding model %r loaded.", _MODEL_NAME)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def embed(text: str) -> np.ndarray:
    """Encode *text* into a unit-normalised embedding vector.

    Args:
        text: The string to embed (e.g. the last user message).

    Returns:
        1-D float32 numpy array of shape (384,) — the all-MiniLM-L6-v2
        output dimension.  The vector is L2-normalised so that dot-product
        equals cosine similarity.
    """
    # encode() returns a 2-D array for a list input or 1-D for a single
    # string; we always pass a single string and squeeze to 1-D.
    vector: np.ndarray = _model.encode(
        text,
        normalize_embeddings=True,   # L2-normalise → cosine sim == dot product
        convert_to_numpy=True,
    )
    return vector.astype(np.float32)
