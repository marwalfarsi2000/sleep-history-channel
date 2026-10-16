"""Centralized error classification and retry module."""

from __future__ import annotations

import time
import urllib.error
from typing import Callable, TypeVar, Any

T = TypeVar("T")


class ErrorCategory:
    TRANSIENT_RETRYABLE = "TRANSIENT_RETRYABLE"
    VALIDATION = "VALIDATION"
    CONFIGURATION = "CONFIGURATION"
    PERMANENT = "PERMANENT"


def classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (429, 500, 502, 503, 504):
            return ErrorCategory.TRANSIENT_RETRYABLE
        if exc.code in (401, 403):
            return ErrorCategory.CONFIGURATION
        return ErrorCategory.PERMANENT

    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return ErrorCategory.TRANSIENT_RETRYABLE

    if any(k in msg for k in ("transient", "http 429", "http 500", "http 502", "http 503", "http 504", "rate limit")):
        return ErrorCategory.TRANSIENT_RETRYABLE

    if any(k in msg for k in ("validation", "mismatch", "failed", "qc_failed", "diacritization", "speech_safe")):
        return ErrorCategory.VALIDATION

    if any(k in msg for k in ("missing", "credential", "configuration", "not configured")):
        return ErrorCategory.CONFIGURATION

    return ErrorCategory.PERMANENT


def retry_transient(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    **kwargs: Any,
) -> T:
    delay = initial_delay
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            category = classify_error(exc)
            if category == ErrorCategory.TRANSIENT_RETRYABLE and attempt < max_retries:
                time.sleep(delay)
                delay *= backoff_factor
            else:
                raise exc
    if last_exc:
        raise last_exc
    raise RuntimeError("Retry failed without exception")
