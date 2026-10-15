from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


class VoiceLockTests(unittest.TestCase):
    def test_exhausted_engine_restarts_whole_episode_with_next_engine(self):
        calls = []

        async def fake_tts_one(text, out, engine=""):
            calls.append((out.stem, engine))
            if engine == "model-a" and out.stem == "002":
                raise pipeline.TtsEngineExhausted(engine, "daily free quota exhausted")
            out.write_bytes(b"ID3" + b"0" * 2048)
            return engine

        with tempfile.TemporaryDirectory() as folder:
            story = Path(folder)
            (story / "logs").mkdir()

            def fake_prepare(target):
                chunks = target / "tts_chunks"
                chunks.mkdir(exist_ok=True)
                for name in ("001", "002", "003"):
                    (chunks / f"{name}.txt").write_text(f"chunk {name}", encoding="utf-8")

            settings = dict(pipeline.SETTINGS, tts_provider="gemini")
            with mock.patch.object(pipeline, "SETTINGS", settings), \
                 mock.patch.object(pipeline, "validate_gemini_voice"), \
                 mock.patch.object(pipeline, "prepare", fake_prepare), \
                 mock.patch.object(pipeline, "tts_engines", return_value=["model-a", "model-b"]), \
                 mock.patch.object(pipeline, "tts_one", fake_tts_one), \
                 mock.patch.object(pipeline, "run"), \
                 mock.patch.object(pipeline, "mix_background_audio"), \
                 mock.patch.object(pipeline, "validate_audio"), \
                 mock.patch.object(pipeline, "duration", return_value=1.0):
                asyncio.run(pipeline.synthesize(story))

            models = json.loads((story / "logs" / "tts_chunk_models.json").read_text(encoding="utf-8"))
            lock = json.loads((story / "logs" / "tts_voice_lock.json").read_text(encoding="utf-8"))

        self.assertEqual(set(models.values()), {"model-b"})
        self.assertEqual(lock, {"engine": "model-b"})
        # Chunk 001 was regenerated with model-b; nothing from model-a survives.
        self.assertEqual(calls[-3:], [("001", "model-b"), ("002", "model-b"), ("003", "model-b")])


if __name__ == "__main__":
    unittest.main()
