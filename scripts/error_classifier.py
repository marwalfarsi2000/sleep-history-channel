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

    if isinstance(exc, FileNotFoundError):
        return ErrorCategory.PERMANENT

    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (429, 500, 502, 503, 504):
            return ErrorCategory.TRANSIENT_RETRYABLE
        if exc.code in (401, 403):
            return ErrorCategory.CONFIGURATION
        return ErrorCategory.PERMANENT

    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return ErrorCategory.TRANSIENT_RETRYABLE

    if isinstance(exc, OSError):
        if any(term in msg for term in ("network", "connection", "timed out", "reset", "refused", "temporary")):
            return ErrorCategory.TRANSIENT_RETRYABLE
        return ErrorCategory.PERMANENT

    if any(k in msg for k in ("rate limit", "http 429", "http 500", "http 502", "http 503", "http 504", "temporary failure", "transient")):
        return ErrorCategory.TRANSIENT_RETRYABLE

    if any(k in msg for k in ("missing", "credential", "not configured", "invalid api key", "unapproved llm", "unapproved tts")):
        return ErrorCategory.CONFIGURATION

    if any(k in msg for k in ("validation_failed", "qc_failed", "speech_safe_validation_failed", "diacritization_failed", "transcript mismatch", "fidelity")):
        return ErrorCategory.VALIDATION

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
