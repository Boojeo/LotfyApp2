"""Backoff, jitter and a circuit breaker.

A trade manager that hammers a broker after a drop is worse than one that
waits: rate limits get you locked out exactly when you need to move a stop.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from ..errors import CircuitOpenError, RateLimitError, RetryableError

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryPolicy:
    attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.25

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with proportional jitter (attempt is 1-based)."""
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return raw * (1.0 + random.uniform(-self.jitter, self.jitter))


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    description: str = "request",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn``, retrying only :class:`RetryableError`.

    A :class:`RateLimitError` carrying ``retry_after`` overrides the computed
    backoff -- the server knows better than we do.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return fn()
        except RateLimitError as exc:
            last = exc
            wait = exc.retry_after if exc.retry_after is not None else policy.delay_for(attempt)
        except RetryableError as exc:
            last = exc
            wait = policy.delay_for(attempt)
        if attempt == policy.attempts:
            break
        log.warning(
            "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
            description, attempt, policy.attempts, last, wait,
        )
        sleep(wait)
    assert last is not None
    raise last


class CircuitBreaker:
    """Trips after ``threshold`` consecutive failures, recovers after ``cooldown``.

    While open, calls raise immediately instead of piling onto a broker that is
    already unhappy.  The supervisor treats an open circuit as "degraded": it
    keeps the position state it has and refuses to make new decisions.
    """

    def __init__(self, threshold: int = 5, cooldown: float = 60.0, name: str = "broker"):
        self.threshold = threshold
        self.cooldown = cooldown
        self.name = name
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._is_open_locked()

    def _is_open_locked(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.cooldown:
            # Half-open: let exactly one call through to probe recovery.
            self._opened_at = None
            self._failures = self.threshold - 1
            return False
        return True

    def guard(self) -> None:
        if self.is_open:
            raise CircuitOpenError(f"{self.name} circuit open; skipping call")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                log.error(
                    "%s circuit OPEN after %d consecutive failures (cooldown %.0fs)",
                    self.name, self._failures, self.cooldown,
                )

    def call(self, fn: Callable[[], T]) -> T:
        self.guard()
        try:
            result = fn()
        except RetryableError:
            self.record_failure()
            raise
        self.record_success()
        return result
