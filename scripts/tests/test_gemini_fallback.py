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
    def test_thinking_config_for_model_uses_low_for_gemini_3_8_flash(self):
        config = generate_daily.thinking_config_for_model("gemini-3.8-flash")
        self.assertEqual(config, {"thinkingLevel": "low"})

    def test_thinking_config_for_model_uses_minimal_for_gemini_3_family(self):
        config = generate_daily.thinking_config_for_model("gemini-3.5-flash-lite")
        self.assertEqual(config, {"thinkingLevel": "minimal"})
        config = generate_daily.thinking_config_for_model("gemini-3.0-flash")
        self.assertEqual(config, {"thinkingLevel": "minimal"})

    def test_thinking_config_for_model_uses_budget_for_non_gemini_3(self):
        config = generate_daily.thinking_config_for_model("gemini-2.5-flash")
        self.assertEqual(config, {"thinkingBudget": 0})
        config = generate_daily.thinking_config_for_model("gemini-3.1-flash-lite")
        self.assertEqual(config, {"thinkingBudget": 0})

    def test_transient_primary_failure_switches_to_free_writing_model(self):
        success = {"candidates": [{"content": {"parts": [{"text": "مخطط ناجح"}]}}]}
        responses = [
            _http_error(503, '{"error":{"status":"UNAVAILABLE"}}'),
            _Response(success),
        ]
        with mock.patch.object(generate_daily, "SETTINGS", {
            "llm_fallback_models": ["gemini-3.5-flash-lite"],
            "cost_policy": "free_or_preapproved_only",
            "approved_llm_models": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
        }), mock.patch.object(generate_daily, "MODEL", "gemini-3.1-flash-lite"), \
             mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "test"}), \
             mock.patch.object(generate_daily, "GEMINI_RETRY_DELAYS", ()), \
             mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=responses) as urlopen:
            text, _sources = generate_daily.gemini("prompt")

        self.assertEqual(text, "مخطط ناجح")
        self.assertEqual(urlopen.call_count, 2)
        requested_urls = [call.args[0].full_url for call in urlopen.call_args_list]
        self.assertIn("gemini-3.1-flash-lite", requested_urls[0])
        self.assertIn("gemini-3.5-flash-lite", requested_urls[1])
        fallback_payload = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertEqual(
            fallback_payload["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "minimal"},
        )

    def test_search_rate_limit_is_not_hammered_and_fails_closed_without_model_memory_fallback(self):
        responses = [_http_error(429, '{"error":{"status":"RESOURCE_EXHAUSTED"}}') for _ in range(4)]
        with mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "test"}), \
             mock.patch.object(generate_daily, "GEMINI_RETRY_DELAYS", (0, 0, 0)), \
             mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=responses) as urlopen, \
             mock.patch.object(generate_daily.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "GROUNDED_RESEARCH_UNAVAILABLE"):
                generate_daily._gemini_one("model", "prompt", search=True)

        # A 429 is classified once and left to the router; it is never retried in place.
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        payload = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
        self.assertIn("tools", payload)

    def test_daily_quota_stops_without_sleeping(self):
        errors = [
            _http_error(
                429,
                '{"error":{"status":"RESOURCE_EXHAUSTED",'
                '"message":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}',
            )
            for _ in range(len(generate_daily._llm_models()))
        ]
        with mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "test"}), \
             mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=errors), \
             mock.patch.object(generate_daily.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "daily quota"):
                generate_daily.gemini("prompt")

        sleep.assert_not_called()

    def test_retired_model_is_skipped_and_research_fallbacks_only_for_search(self):
        success = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        responses = [_http_error(404, '{"error":{"status":"NOT_FOUND"}}'), _Response(success)]
        settings = {
            "llm_fallback_models": ["gemini-3.1-flash-lite"],
            "research_fallback_models": ["gemini-2.5-flash"],
            "cost_policy": "free_or_preapproved_only",
            "approved_llm_models": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"],
        }
        with mock.patch.object(generate_daily, "SETTINGS", settings), \
             mock.patch.object(generate_daily, "MODEL", "gemini-3.5-flash-lite"):
            self.assertEqual(generate_daily._llm_models(), ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
            self.assertEqual(generate_daily._llm_models(search=True)[-1], "gemini-2.5-flash")
            with mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "test"}), \
                 mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=responses) as urlopen:
                text, _ = generate_daily.gemini("prompt")
        self.assertEqual(text, "ok")
        self.assertIn("gemini-3.1-flash-lite", urlopen.call_args_list[1].args[0].full_url)

    def test_unsupported_thinking_level_400_retries_with_low_once(self):
        success = {"candidates": [{"content": {"parts": [{"text": "recovered"}]}}]}
        unsupported_error = _http_error(
            400,
            '{"error":{"status":"INVALID_ARGUMENT","message":"Thinking level MINIMAL is not supported for this model. Please retry with other thinking level."}}',
        )
        call_count = [0]
        def side_effect_func(request, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise unsupported_error
            return _Response(success)
        with mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "test"}), \
             mock.patch.object(generate_daily, "GEMINI_RETRY_DELAYS", ()), \
             mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=side_effect_func) as urlopen:
            text, _ = generate_daily._gemini_one("gemini-3.5-flash-lite", "prompt")

        self.assertEqual(text, "recovered")
        self.assertEqual(urlopen.call_count, 2)
        first_payload = json.loads(urlopen.call_args_list[0].args[0].data.decode("utf-8"))
        self.assertEqual(
            first_payload["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "minimal"},
        )
        second_payload = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertEqual(
            second_payload["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "low"},
        )

    def test_other_400_errors_fail_fast_without_retry(self):
        invalid_error = _http_error(
            400,
            '{"error":{"status":"INVALID_ARGUMENT","message":"Invalid request"}}',
        )
        with mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "test"}), \
             mock.patch.object(generate_daily, "GEMINI_RETRY_DELAYS", ()), \
             mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=[invalid_error]):
            with self.assertRaisesRegex(RuntimeError, "Gemini HTTP 400"):
                generate_daily._gemini_one("gemini-3.8-flash", "prompt")


if __name__ == "__main__":
    unittest.main()
