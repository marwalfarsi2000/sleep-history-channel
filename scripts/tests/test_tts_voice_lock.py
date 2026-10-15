"""Permanent voice rules for every episode (settings: voice_policy).

1. Generated chunks are kept until the episode finishes (checkpoint).
2. Gemini TTS models share one narrator and may alternate inside an episode.
3. When Gemini quota runs out the episode waits and resumes with the same narrator.
4. Google Cloud voices are an emergency fallback after N days with no Gemini progress.
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
    def run_synthesize(self, story: Path, fake_tts_one, *, day: str = "2026-10-15", cloud=(CLOUD,)):
        def fake_prepare(target):
            chunks = target / "tts_chunks"
            chunks.mkdir(exist_ok=True)
            for name in ("001", "002", "003"):
                (chunks / f"{name}.txt").write_text(f"chunk {name}", encoding="utf-8")

        settings = dict(pipeline.SETTINGS, tts_provider="gemini")
        with mock.patch.object(pipeline, "SETTINGS", settings), \
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

    def test_quota_exhaustion_keeps_chunks_and_waits_for_same_narrator(self):
        async def fake(text, out, engine=""):
            if out.stem == "003":
                raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        with tempfile.TemporaryDirectory() as folder:
            story = Path(folder)
            (story / "logs").mkdir()
            with self.assertRaisesRegex(RuntimeError, "TTS_WAITING_FOR_QUOTA"):
                self.run_synthesize(story, fake)
            state = json.loads((story / "logs" / "tts_voice_state.json").read_text(encoding="utf-8"))
            kept = sorted(path.name for path in (story / "audio_chunks").glob("*.mp3"))
        self.assertEqual(state["voice"], pipeline.GEMINI_VOICE)
        self.assertEqual(kept, ["001.mp3", "002.mp3"])
        self.assertEqual(state["days_without_gemini_quota"], [])

    def test_resume_next_day_reuses_saved_chunks(self):
        calls = []

        async def exhausted_on_third(text, out, engine=""):
            if out.stem == "003":
                raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        async def fresh_quota(text, out, engine=""):
            calls.append(out.stem)
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        with tempfile.TemporaryDirectory() as folder:
            story = Path(folder)
            (story / "logs").mkdir()
            with self.assertRaises(RuntimeError):
                self.run_synthesize(story, exhausted_on_third)
            self.run_synthesize(story, fresh_quota, day="2026-10-16")
            models = self.models(story)
        self.assertEqual(calls, ["003"])
        self.assertEqual(set(models.values()), {"model-a"})

    def test_emergency_cloud_voice_only_after_two_days_without_gemini_progress(self):
        async def no_gemini(text, out, engine=""):
            if not engine.startswith("google-cloud:"):
                raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        with tempfile.TemporaryDirectory() as folder:
            story = Path(folder)
            (story / "logs").mkdir()
            with self.assertRaisesRegex(RuntimeError, "TTS_WAITING_FOR_QUOTA"):
                self.run_synthesize(story, no_gemini, day="2026-10-15")
            self.run_synthesize(story, no_gemini, day="2026-10-16")
            models = self.models(story)
            state = json.loads((story / "logs" / "tts_voice_state.json").read_text(encoding="utf-8"))
        self.assertEqual(set(models.values()), {CLOUD})
        self.assertEqual(state["voice"], CLOUD)

    def test_switch_to_cloud_discards_gemini_chunks_so_voices_never_mix(self):
        stage = {"day": 1}

        async def fake(text, out, engine=""):
            if engine.startswith("google-cloud:"):
                out.write_bytes(b"ID3" + b"0" * 2048)
                return engine
            if stage["day"] == 1 and out.stem == "001":
                out.write_bytes(b"ID3" + b"0" * 2048)
                return engine
            raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")

        with tempfile.TemporaryDirectory() as folder:
            story = Path(folder)
            (story / "logs").mkdir()
            with self.assertRaises(RuntimeError):
                self.run_synthesize(story, fake, day="2026-10-15")  # progress made: counter stays empty
            stage["day"] = 2
            with self.assertRaises(RuntimeError):
                self.run_synthesize(story, fake, day="2026-10-16")  # no progress: day 1
            self.run_synthesize(story, fake, day="2026-10-17")      # no progress: day 2 -> emergency
            models = self.models(story)
        self.assertEqual(set(models.values()), {CLOUD})


if __name__ == "__main__":
    unittest.main()
