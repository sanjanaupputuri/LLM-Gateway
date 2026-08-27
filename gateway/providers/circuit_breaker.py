# gateway/providers/circuit_breaker.py
# Per-provider in-memory circuit breaker.
# Spec: docs/02_ARCHITECTURE.md §7
#
# States: closed (normal) -> open (skip) -> half_open (single trial)
# - failure_threshold consecutive ProviderErrors -> open
# - After cooldown_seconds -> half_open (one trial allowed)
# - half_open success -> closed, reset count
# - half_open failure -> open, reset opened_at

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import ClassVar


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Circuit breaker for a single provider.

    Thread-safe via a per-instance Lock — safe for use with FastAPI's
    async workers (which run in a thread pool for sync code paths).
    """

    provider: str
    failure_threshold: int = 3
    cooldown_seconds: int = 60

    # Internal state — not exposed in constructor
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False, repr=False)
    _failure_count: int = field(default=0, init=False, repr=False)
    _opened_at: float | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def state(self) -> BreakerState:
        """Current state, with automatic half_open transition check."""
        with self._lock:
            return self._get_state_locked()

    def _get_state_locked(self) -> BreakerState:
        """Must be called with self._lock held."""
        if self._state == BreakerState.OPEN:
            if self._opened_at is not None:
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.cooldown_seconds:
                    self._state = BreakerState.HALF_OPEN
        return self._state

    def is_open(self) -> bool:
        """Return True if this provider should be SKIPPED (state is OPEN).

        HALF_OPEN returns False — exactly one trial request is allowed through.
        The caller (router) must call record_success/record_failure after the trial.
        """
        with self._lock:
            return self._get_state_locked() == BreakerState.OPEN

    def record_success(self) -> None:
        """Call after a successful provider response."""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            self._opened_at = None

    def record_failure(self) -> None:
        """Call after a ProviderError. Increments count and opens if threshold reached."""
        with self._lock:
            self._failure_count += 1
            if self._state == BreakerState.HALF_OPEN:
                # Trial failed — go back to open, reset cooldown timer
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
            elif self._failure_count >= self.failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Fully reset to closed state — useful in tests."""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            self._opened_at = None

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(provider={self.provider!r}, "
            f"state={self._state.value}, failures={self._failure_count})"
        )


# ---------------------------------------------------------------------------
# Module-level registry — one breaker per provider, shared across all requests
# in the process lifetime. Reset on gateway restart (in-memory only by design).
# ---------------------------------------------------------------------------

_registry: dict[str, CircuitBreaker] = {}
_registry_lock = Lock()


def get_breaker(
    provider: str,
    failure_threshold: int = 3,
    cooldown_seconds: int = 60,
) -> CircuitBreaker:
    """Get or create the CircuitBreaker for a provider.

    Thread-safe. Parameters only apply on first creation — subsequent calls
    for the same provider return the existing instance regardless of params.

    In tests, call `reset_all_breakers()` between tests to ensure isolation.
    """
    with _registry_lock:
        if provider not in _registry:
            _registry[provider] = CircuitBreaker(
                provider=provider,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
            )
        return _registry[provider]


def reset_all_breakers() -> None:
    """Reset all registered circuit breakers to closed state.

    Call this in test teardown (or autouse fixture) to prevent state leakage
    between tests.
    """
    with _registry_lock:
        for breaker in _registry.values():
            breaker.reset()
        _registry.clear()
