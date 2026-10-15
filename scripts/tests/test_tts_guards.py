from __future__ import annotations

import json
import base64
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import pipeline


class _FakeResponse:
    def __init__(self, body: bytes = b"ID3-test-audio"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class TtsGuardTests(unittest.TestCase):
    def test_pausal_taa_marbuta_drops_only_final_marks(self):
        source = "في مَدِينَةٍ عَمِيقَةٍ، كانَتِ الرِّحْلَةُ عَمِيقَةً؟"
        result = pipeline.api_text(source)
        self.assertIn("مَدِينَةٍ عَمِيقَةٍ،", result)
        self.assertTrue(result.endswith("عَمِيقَة؟"))
        self.assertNotRegex(
            result,
            r"ة[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]+(?=\s*[.!؟…؛]|\s*$)",
        )

    def test_fish_request_uses_native_speed(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeResponse()

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sample.mp3"
            with mock.patch.dict(
                pipeline.SECRETS,
                {"FISH_API_KEY": "test-key", "FISH_VOICE_ID": "test-voice"},
                clear=False,
            ), mock.patch.object(pipeline.urllib.request, "urlopen", fake_urlopen):
                pipeline.fish_tts_one("نَصٌّ هادِئٌ.", output)

        self.assertNotIn("speed", captured["payload"])
        self.assertEqual(captured["payload"]["reference_id"], "test-voice")

    def test_config_enforces_native_speed_without_slowdown(self):
        self.assertEqual(float(pipeline.SETTINGS["tts_generation_speed"]), 1.0)
        self.assertEqual(float(pipeline.SETTINGS["post_tts_tempo"]), 1.0)

    def test_fish_voice_identity_must_match_selected_voice(self):
        response = _FakeResponse(json.dumps({"title": "ناشونال جيوغرافيك"}, ensure_ascii=False).encode("utf-8"))
        with mock.patch.dict(
            pipeline.SECRETS,
            {"FISH_API_KEY": "test-key", "FISH_VOICE_ID": "test-voice"},
            clear=False,
        ), mock.patch.object(pipeline.urllib.request, "urlopen", return_value=response):
            pipeline.validate_fish_voice()

    def test_fish_voice_identity_rejects_wrong_voice(self):
        response = _FakeResponse(json.dumps({"title": "Wrong Voice"}).encode("utf-8"))
        with mock.patch.dict(
            pipeline.SECRETS,
            {"FISH_API_KEY": "test-key", "FISH_VOICE_ID": "test-voice"},
            clear=False,
        ), mock.patch.object(pipeline.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Fish voice mismatch"):
                pipeline.validate_fish_voice()

    def test_munsit_request_uses_native_speed_and_wav_conversion(self):
        captured = {}

        class _Headers(dict):
            def get(self, key, default=None):
                return super().get(key, default)

        class _MunsitResponse(_FakeResponse):
            headers = _Headers({"Content-Type": "audio/wav"})

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _MunsitResponse(b"RIFF" + b"0" * 2048)

        def fake_run(command, check=True):
            Path(command[-1]).write_bytes(b"ID3" + b"0" * 2048)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sample.mp3"
            with mock.patch.dict(
                pipeline.SECRETS,
                {"MUNSIT_API_KEY": "test-key", "MUNSIT_VOICE_ID": "test-voice"},
                clear=False,
            ), mock.patch.object(pipeline.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
                pipeline, "run", fake_run
            ):
                pipeline.munsit_tts_one("نَصٌّ هادِئٌ.", output)
            self.assertTrue(output.exists())

        self.assertEqual(captured["payload"]["speed"], 1.0)
        self.assertFalse(captured["payload"]["streaming"])
        self.assertEqual(captured["payload"]["sample_rate"], 48000)

    def test_munsit_voice_identity_must_match_selected_voice(self):
        with mock.patch.dict(
            pipeline.SECRETS,
            {"MUNSIT_API_KEY": "test-key", "MUNSIT_VOICE_ID": "voice-jadd"},
            clear=False,
        ), mock.patch.object(
            pipeline,
            "munsit_voices",
            return_value=[{"voice_id": "voice-jadd", "name": "جاد"}],
        ):
            pipeline.validate_munsit_voice()

    def test_gemini_request_uses_structured_style_and_custom_voice(self):
        captured = {}
        wav = b"RIFF" + b"0" * 2048
        response = _FakeResponse(json.dumps({
            "output_audio": {"data": base64.b64encode(wav).decode("ascii")}
        }).encode("utf-8"))

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return response

        def fake_run(command, check=True):
            Path(command[-1]).write_bytes(b"ID3" + b"0" * 2048)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sample.mp3"
            with mock.patch.dict(
                pipeline.SECRETS,
                {"GEMINI_API_KEY": "test-key", "GEMINI_TTS_VOICE_ID": "voice-test"},
                clear=False,
            ), mock.patch.object(pipeline.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
                pipeline, "run", fake_run
            ):
                pipeline.gemini_tts_one("نَصٌّ هادِئٌ.", output, "gemini-3.8-flash-tts")
            self.assertTrue(output.exists())

        payload = captured["payload"]
        self.assertEqual(payload["model"], "gemini-3.8-flash-tts")
        self.assertEqual(payload["generation_config"]["speech_config"][0]["voice"], "voice-test")
        content = payload["input"][0]["content"][0]
        self.assertEqual(content["text"], "نَصٌّ هادِئٌ.")
        self.assertEqual(content["annotations"][0]["type"], "speech_metadata")

    def test_gemini_retries_minute_quota_then_succeeds(self):
        wav = b"RIFF" + b"0" * 2048
        response = _FakeResponse(json.dumps({
            "output_audio": {"data": base64.b64encode(wav).decode("ascii")}
        }).encode("utf-8"))
        minute_error = urllib.error.HTTPError(
            "https://example.invalid", 429, "Too Many Requests",
            {"Retry-After": "0"}, io.BytesIO(b'{"quotaId":"GenerateRequestsPerMinutePerProjectPerModel"}')
        )
        calls = [minute_error, response]

        def fake_urlopen(_request, timeout):
            item = calls.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def fake_run(command, check=True):
            Path(command[-1]).write_bytes(b"ID3" + b"0" * 2048)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sample.mp3"
            with mock.patch.dict(
                pipeline.SECRETS,
                {"GEMINI_API_KEY": "test-key", "GEMINI_TTS_VOICE_ID": "voice-test"},
                clear=False,
            ), mock.patch.object(pipeline.urllib.request, "urlopen", fake_urlopen), mock.patch.object(
                pipeline.time, "sleep"
            ) as sleep, mock.patch.object(pipeline, "run", fake_run):
                pipeline.gemini_tts_one("نَصٌّ هادِئٌ.", output, "gemini-3.8-flash-tts")
            self.assertTrue(output.exists())
            sleep.assert_called_once_with(60)

    def test_gemini_daily_quota_stops_without_retry(self):
        def daily_error():
            return urllib.error.HTTPError(
                "https://example.invalid", 429, "Too Many Requests", {},
                io.BytesIO(b'{"quotaId":"GenerateRequestsPerDayPerProjectPerModel"}')
            )
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sample.mp3"
            with mock.patch.dict(
                pipeline.SECRETS,
                {"GEMINI_API_KEY": "test-key", "GEMINI_TTS_VOICE_ID": "voice-test"},
                clear=False,
            ), mock.patch.object(
                pipeline.urllib.request, "urlopen", side_effect=[daily_error(), daily_error()]
            ), mock.patch.object(pipeline.time, "sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "daily free quota exhausted"):
                    pipeline.gemini_tts_one("نَصٌّ هادِئٌ.", output, "gemini-3.8-flash-tts")
            sleep.assert_not_called()

    def test_gemini_daily_quota_never_switches_model_inside_a_chunk(self):
        daily_error = urllib.error.HTTPError(
            "https://example.invalid", 429, "Too Many Requests", {},
            io.BytesIO(b'{"quotaId":"GenerateRequestsPerDayPerProjectPerModel"}')
        )
        requested_models = []

        def fake_urlopen(request, timeout):
            requested_models.append(json.loads(request.data.decode("utf-8"))["model"])
            raise daily_error

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sample.mp3"
            with mock.patch.dict(
                pipeline.SECRETS,
                {"GEMINI_API_KEY": "test-key", "GEMINI_TTS_VOICE_ID": "voice-test"},
                clear=False,
            ), mock.patch.object(pipeline.urllib.request, "urlopen", fake_urlopen):
                with self.assertRaises(pipeline.TtsEngineExhausted):
                    pipeline.gemini_tts_one("text", output, "gemini-3.8-flash-tts")

        # No in-chunk switch: the episode-level lock in synthesize() picks the next engine.
        self.assertEqual(requested_models, ["gemini-3.8-flash-tts"])

    def test_google_cloud_cap_blocks_before_any_request(self):
        with tempfile.TemporaryDirectory() as folder:
            usage = Path(folder) / "tts_usage.json"
            usage.write_text(json.dumps({pipeline._usage_month(): {"wavenet": 899_990}}), encoding="utf-8")
            with mock.patch.object(pipeline, "TTS_USAGE_PATH", usage), \
                 mock.patch.dict(pipeline.SECRETS, {"GOOGLE_CLOUD_TTS_API_KEY": "test"}, clear=False), \
                 mock.patch.object(pipeline.urllib.request, "urlopen") as urlopen:
                with self.assertRaisesRegex(pipeline.TtsEngineExhausted, "cap"):
                    pipeline.google_cloud_tts_one(
                        "longer than the remaining allowance",
                        Path(folder) / "x.mp3",
                        {"name": "ar-XA-Wavenet-B", "tier": "wavenet"},
                    )
            urlopen.assert_not_called()

    def test_google_cloud_request_pieces_respect_byte_limit(self):
        text = ("جُمْلَةٌ هادِئَةٌ. " * 400).strip()
        pieces = pipeline._split_utf8(text, 4500)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece.encode("utf-8")) <= 4500 for piece in pieces))
        self.assertEqual(" ".join(pieces).split(), text.split())

    def test_cloud_voices_order_and_immediate_fallback_policy(self):
        with mock.patch.dict(pipeline.SECRETS, {"GOOGLE_CLOUD_TTS_API_KEY": "test"}, clear=False):
            engines = pipeline.tts_cloud_engines()
        self.assertEqual(engines, ["google-cloud:ar-XA-Wavenet-B", "google-cloud:ar-XA-Standard-B"])
        policy = pipeline.SETTINGS["voice_policy"]
        self.assertTrue(policy["gemini_models_share_one_narrator"])
        self.assertTrue(policy["immediate_cloud_fallback"])
        self.assertTrue(policy["skip_exhausted_gemini_for_quota_day"])
        self.assertTrue(policy["never_mix_gemini_and_cloud"])

    def test_gemini_uses_larger_free_quota_chunks(self):
        sentence = "هٰذِهِ جُمْلَةٌ تاريخيةٌ هادئةٌ ومُشَكَّلَةٌ بعناية."
        text = " ".join([sentence] * 220)
        old_chunks = pipeline.split_chunks(text, limit=2600)
        new_chunks = pipeline.split_chunks(text, limit=int(pipeline.SETTINGS["gemini_tts_chunk_char_limit"]))
        limit = int(pipeline.SETTINGS["gemini_tts_chunk_char_limit"])
        self.assertLess(len(new_chunks), len(old_chunks))
        self.assertTrue(all(len(chunk) <= limit for chunk in new_chunks))
        # ~7 minutes of narration per request keeps a margin below the
        # 16,384 output-token limit (about 8.5 minutes of audio).
        self.assertLessEqual(limit, 6000)

    def test_gemini_voice_identity_must_match_selected_voice(self):
        response = _FakeResponse(json.dumps({
            "id": "voice-test", "display_name": "راوي القصص 1"
        }, ensure_ascii=False).encode("utf-8"))
        with mock.patch.dict(
            pipeline.SECRETS,
            {"GEMINI_API_KEY": "test-key", "GEMINI_TTS_VOICE_ID": "voice-test"},
            clear=False,
        ), mock.patch.object(pipeline.urllib.request, "urlopen", return_value=response):
            pipeline.validate_gemini_voice()


if __name__ == "__main__":
    unittest.main()
