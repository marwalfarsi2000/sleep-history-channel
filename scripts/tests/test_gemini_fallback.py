from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

import generate_daily


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com",
        code,
        "error",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


class GeminiFallbackTests(unittest.TestCase):
    def test_search_quota_falls_back_immediately_and_retries_transient_503(self):
        success = {"candidates": [{"content": {"parts": [{"text": "بحث موثوق"}]}}]}
        responses = [
            _http_error(429, '{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
            _http_error(503, '{"error":{"status":"UNAVAILABLE"}}'),
            _Response(success),
        ]
        with mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "test"}), \
             mock.patch.object(generate_daily, "GEMINI_RETRY_DELAYS", (0,)), \
             mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=responses) as urlopen, \
             mock.patch.object(generate_daily.time, "sleep") as sleep:
            text, _sources = generate_daily.gemini("prompt", search=True)

        self.assertEqual(text, "بحث موثوق")
        self.assertEqual(urlopen.call_count, 3)
        sleep.assert_called_once_with(0)

    def test_daily_quota_stops_without_sleeping(self):
        error = _http_error(
            429,
            '{"error":{"status":"RESOURCE_EXHAUSTED",'
            '"message":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}',
        )
        with mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "test"}), \
             mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=error), \
             mock.patch.object(generate_daily.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "daily quota"):
                generate_daily.gemini("prompt")

        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
