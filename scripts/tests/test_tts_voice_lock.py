"""Permanent voice rules for every episode (settings: voice_policy).

1. Generated chunks are kept until the episode finishes (checkpoint).
2. Gemini TTS models share one narrator and may alternate inside an episode.
3. When Gemini quota runs out the episode switches immediately to Google Cloud.
4. Later episodes skip Gemini for the rest of the same quota day.
5. One episode never mixes the Gemini narrator with a Google Cloud voice.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline

CLOUD = "google-cloud:ar-XA-Wavenet-B"


class VoicePolicyTests(unittest.TestCase):
    def run_synthesize(
        self, story: Path, fake_tts_one, *, day: str = "2026-10-15", cloud=(CLOUD,), quota_state=None
    ):
        def fake_prepare(target):
            chunks = target / "tts_chunks"
            chunks.mkdir(exist_ok=True)
            for name in ("001", "002", "003"):
                (chunks / f"{name}.txt").write_text(f"chunk {name}", encoding="utf-8")

        settings = dict(pipeline.SETTINGS, tts_provider="gemini")
        quota_state = quota_state or story / ".test_tts_quota_state.json"
        with mock.patch.object(pipeline, "SETTINGS", settings), \
             mock.patch.object(pipeline, "TTS_QUOTA_STATE_PATH", quota_state), \
             mock.patch.object(pipeline, "validate_gemini_voice"), \
             mock.patch.object(pipeline, "prepare", fake_prepare), \
             mock.patch.object(pipeline, "_gemini_tts_models", return_value=["model-a", "model-b"]), \
             mock.patch.object(pipeline, "tts_cloud_engines", return_value=list(cloud)), \
             mock.patch.object(pipeline, "_google_cloud_voices", return_value=[{"name": "ar-XA-Wavenet-B", "tier": "wavenet"}]), \
             mock.patch.object(pipeline, "google_cloud_remaining", return_value=10**6), \
             mock.patch.object(pipeline, "quota_day", return_value=day), \
             mock.patch.object(pipeline, "tts_one", fake_tts_one), \
             mock.patch.object(pipeline, "run"), \
             mock.patch.object(pipeline, "mix_background_audio"), \
             mock.patch.object(pipeline, "validate_audio"), \
             mock.patch.object(pipeline, "duration", return_value=1.0):
            asyncio.run(pipeline.synthesize(story))

    @staticmethod
    def models(story: Path) -> dict:
        return json.loads((story / "logs" / "tts_chunk_models.json").read_text(encoding="utf-8"))

    def test_gemini_models_alternate_inside_one_episode_without_restarting(self):
        calls = []

        async def fake(text, out, engine=""):
            calls.append((out.stem, engine))
            if engine == "model-a" and out.stem != "001":
                raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        with tempfile.TemporaryDirectory() as folder:
            story = Path(folder)
            (story / "logs").mkdir()
            self.run_synthesize(story, fake)
            models = self.models(story)
        # Chunk 001 is kept (no restart); the rest continue with model-b.
        self.assertEqual(models, {"001.txt": "model-a", "002.txt": "model-b", "003.txt": "model-b"})
        self.assertNotIn(("001", "model-b"), calls)

    def test_quota_exhaustion_switches_immediately_and_never_mixes_narrators(self):
        async def fake(text, out, engine=""):
            if not engine.startswith("google-cloud:") and out.stem == "003":
                raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        with tempfile.TemporaryDirectory() as folder:
            story = Path(folder)
            (story / "logs").mkdir()
            self.run_synthesize(story, fake)
            state = json.loads((story / "logs" / "tts_voice_state.json").read_text(encoding="utf-8"))
            models = self.models(story)
        self.assertEqual(state["voice"], CLOUD)
        self.assertEqual(set(models.values()), {CLOUD})

    def test_next_episode_same_day_skips_gemini(self):
        calls = []

        async def fake(text, out, engine=""):
            calls.append(engine)
            if not engine.startswith("google-cloud:"):
                raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            quota_state = root / "tts_quota_state.json"
            first, second = root / "first", root / "second"
            for story in (first, second):
                (story / "logs").mkdir(parents=True)
            self.run_synthesize(first, fake, quota_state=quota_state)
            calls.clear()
            self.run_synthesize(second, fake, quota_state=quota_state)
            models = self.models(second)
        self.assertTrue(calls)
        self.assertTrue(all(engine == CLOUD for engine in calls))
        self.assertEqual(set(models.values()), {CLOUD})

    def test_new_quota_day_tries_gemini_again(self):
        calls = []

        async def fake(text, out, engine=""):
            calls.append(engine)
            if engine.startswith("google-cloud:"):
                out.write_bytes(b"ID3" + b"0" * 2048)
                return engine
            if current_day["value"] == "2026-10-15":
                raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        current_day = {"value": "2026-10-15"}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            quota_state = root / "tts_quota_state.json"
            first, second = root / "first", root / "second"
            for story in (first, second):
                (story / "logs").mkdir(parents=True)
            self.run_synthesize(first, fake, day=current_day["value"], quota_state=quota_state)
            current_day["value"] = "2026-10-16"
            calls.clear()
            self.run_synthesize(second, fake, day=current_day["value"], quota_state=quota_state)
        self.assertTrue(calls)
        self.assertTrue(all(not engine.startswith("google-cloud:") for engine in calls))


if __name__ == "__main__":
    unittest.main()
