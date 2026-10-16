import urllib.error
import pytest
from error_classifier import ErrorCategory, classify_error, retry_transient


def test_classify_errors():
    http_429 = urllib.error.HTTPError("http://example.com", 429, "Too Many Requests", {}, None)
    assert classify_error(http_429) == ErrorCategory.TRANSIENT_RETRYABLE

    http_401 = urllib.error.HTTPError("http://example.com", 401, "Unauthorized", {}, None)
    assert classify_error(http_401) == ErrorCategory.CONFIGURATION

    val_err = RuntimeError("AUDIO_QC_FAILED: transcript mismatch")
    assert classify_error(val_err) == ErrorCategory.VALIDATION

    cfg_err = RuntimeError("Missing YouTube credential fields")
    assert classify_error(cfg_err) == ErrorCategory.CONFIGURATION


def test_retry_transient_success():
    calls = 0
    def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError("http://example.com", 503, "Unavailable", {}, None)
        return "ok"

    res = retry_transient(flaky, max_retries=3, initial_delay=0.01)
    assert res == "ok"
    assert calls == 2


def test_retry_transient_non_transient_fails_fast():
    calls = 0
    def cfg_fail():
        nonlocal calls
        calls += 1
        raise RuntimeError("Missing secret")

    with pytest.raises(RuntimeError):
        retry_transient(cfg_fail, max_retries=3, initial_delay=0.01)
    assert calls == 1
