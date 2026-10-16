import json
import urllib.error
from unittest.mock import MagicMock, patch
import pytest

import generate_daily


def test_thinking_config_for_model():
    assert generate_daily.thinking_config_for_model("gemini-3.8-flash") == {"thinkingLevel": "low"}
    assert generate_daily.thinking_config_for_model("gemini-3.1-flash-lite") == {"thinkingBudget": 0}
    assert generate_daily.thinking_config_for_model("gemini-1.5-flash") is None


def test_gemini_one_unsupported_thinking_recovery():
    error_400_body = json.dumps({
        "error": {
            "code": 400,
            "message": "Thinking level MINIMAL is not supported for this model. Please retry with other thinking level.",
            "status": "INVALID_ARGUMENT",
        }
    }).encode("utf-8")

    err_400 = urllib.error.HTTPError(
        "http://example.com", 400, "Bad Request", {}, MagicMock(read=lambda: error_400_body)
    )

    success_response = MagicMock()
    success_response.__enter__.return_value = success_response
    success_response.read.return_value = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "Success after fallback"}]}
        }]
    }).encode("utf-8")

    with patch("generate_daily.load_secrets", return_value={"GEMINI_API_KEY": "fake_key"}), \
         patch("urllib.request.urlopen", side_effect=[err_400, success_response]) as mock_urlopen:

        text, _ = generate_daily._gemini_one("gemini-3.8-flash", "test prompt")
        assert text == "Success after fallback"
        assert mock_urlopen.call_count == 2


def test_gemini_one_other_400_fails_fast():
    error_400_body = json.dumps({
        "error": {
            "code": 400,
            "message": "Invalid argument: field X is invalid.",
            "status": "INVALID_ARGUMENT",
        }
    }).encode("utf-8")

    err_400 = urllib.error.HTTPError(
        "http://example.com", 400, "Bad Request", {}, MagicMock(read=lambda: error_400_body)
    )

    with patch("generate_daily.load_secrets", return_value={"GEMINI_API_KEY": "fake_key"}), \
         patch("urllib.request.urlopen", side_effect=err_400) as mock_urlopen:

        with pytest.raises(RuntimeError) as exc_info:
            generate_daily._gemini_one("gemini-3.8-flash", "test prompt")

        assert "Gemini HTTP 400" in str(exc_info.value)
        assert mock_urlopen.call_count == 1
