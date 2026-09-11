# gateway/embeddings.py
# Exposes embed() for encoding text into a unit-normalised embedding vector.
# Model: all-MiniLM-L6-v2 (~80 MB, runs locally, no API cost).
#
# Loading strategy — LAZY (not at import time):
#   The model is initialised on the first call to embed(), not when this
#   module is imported.  This means test files that import gateway.cache or
#   gateway.router (which will import cache in Phase 6) do NOT pay the
#   ~300 ms model-load penalty unless they actually call embed().
#   The single-load guarantee is preserved: once _model is set it is never
#   replaced, so the overhead is paid at most once per process lifetime.
#
# Spec: docs/02_ARCHITECTURE.md §9, docs/04_BUILD_PLAN.md Phase 4

from __future__ import annotations

import logging

import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"

# Module-level holder — None until the first embed() call.
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """Return the shared SentenceTransformer instance, loading it on first call."""
    global _model
    if _model is None:
        logger.info("Loading embedding model %r …", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
        logger.info("Embedding model %r loaded.", _MODEL_NAME)
    return _model


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
    model = _get_model()
    vector: np.ndarray = model.encode(
        text,
        normalize_embeddings=True,   # L2-normalise → cosine sim == dot product
        convert_to_numpy=True,
    )
    return vector.astype(np.float32)
