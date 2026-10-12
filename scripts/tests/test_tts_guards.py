from __future__ import annotations

import json
import base64
import tempfile
import unittest
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
                pipeline.gemini_tts_one("نَصٌّ هادِئٌ.", output)
            self.assertTrue(output.exists())

        payload = captured["payload"]
        self.assertEqual(payload["model"], "gemini-3.8-flash-tts")
        self.assertEqual(payload["generation_config"]["speech_config"][0]["voice"], "voice-test")
        content = payload["input"][0]["content"][0]
        self.assertEqual(content["text"], "نَصٌّ هادِئٌ.")
        self.assertEqual(content["annotations"][0]["type"], "speech_metadata")

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
