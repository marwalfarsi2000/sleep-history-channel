from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.request
from unittest import mock

import pipeline


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode("utf-8")


def _error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "error", {}, io.BytesIO(body))


def _request(model: str) -> urllib.request.Request:
    return urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=k",
        data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
    )


class AudioQcRetryTests(unittest.TestCase):
    def test_transient_503_is_retried_then_succeeds(self):
        with mock.patch.object(pipeline.urllib.request, "urlopen", side_effect=[_error(503), _Response()]) as urlopen, \
             mock.patch.object(pipeline.time, "sleep") as sleep:
            result = pipeline._qc_request_with_fallback(_request("gemini-3.1-flash-lite"), "gemini-3.1-flash-lite")
        self.assertEqual(result["candidates"][0]["content"]["parts"][0]["text"], "ok")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_daily_quota_switches_to_next_approved_model(self):
        daily = _error(429, b'{"quotaId":"GenerateRequestsPerDayPerProjectPerModel"}')
        with mock.patch.object(pipeline.urllib.request, "urlopen", side_effect=[daily, _Response()]) as urlopen, \
             mock.patch.object(pipeline.time, "sleep") as sleep:
            pipeline._qc_request_with_fallback(_request("gemini-3.1-flash-lite"), "gemini-3.1-flash-lite")
        self.assertNotIn("gemini-3.1-flash-lite", urlopen.call_args_list[1].args[0].full_url)
        sleep.assert_not_called()

    def test_qc_stays_mandatory_when_every_model_fails(self):
        with mock.patch.object(pipeline.urllib.request, "urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(_error(400))):
            with self.assertRaisesRegex(RuntimeError, "AUDIO_QC_FAILED"):
                pipeline._qc_request_with_fallback(_request("gemini-3.1-flash-lite"), "gemini-3.1-flash-lite")


if __name__ == "__main__":
    unittest.main()
