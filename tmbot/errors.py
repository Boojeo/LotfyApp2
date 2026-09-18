"""Exception hierarchy.

The split that matters operationally is :class:`RetryableError` vs
:class:`PermanentError`.  Everything that talks to a network draws that line so
the retry helper and the trade engine can make the same decision without
inspecting HTTP status codes themselves.
"""

from __future__ import annotations


class TmbotError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(TmbotError):
    """Configuration is missing or internally inconsistent."""


class BrokerError(TmbotError):
    """Base class for broker failures."""


class RetryableError(BrokerError):
    """Transient failure: connection drop, timeout, 429, 5xx.

    Safe to retry the *same* request.  Callers that mutate state must still
    guard with an idempotency key, because a request can succeed server-side
    and still fail on the way back to us.
    """


class PermanentError(BrokerError):
    """The broker rejected the request and retrying will not help (4xx)."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class AuthError(BrokerError):
    """Session token missing or expired.

    Deliberately NOT a :class:`RetryableError`: repeating the same request with
    the same dead token just burns the rate limit and trips the circuit
    breaker.  The correct response is to re-authenticate once and replay, which
    is what the adapter does.
    """


class RateLimitError(RetryableError):
    """429 from the broker.  Carries the server's requested cool-off."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class CircuitOpenError(RetryableError):
    """The circuit breaker is open; the call was not attempted."""


class StaleDataError(TmbotError):
    """Market data is too old to base a trade-management decision on."""


class NotSupportedError(TmbotError):
    """The connected account/broker cannot perform the requested operation."""
