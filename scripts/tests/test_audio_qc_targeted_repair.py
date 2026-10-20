from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pipeline


def test_regenerate_qc_chunk_tries_same_narrator_fallback_only(tmp_path):
    src = tmp_path / "003.txt"
    out = tmp_path / "003.mp3"
    sibling = tmp_path / "002.mp3"
    src.write_text("نص عربي للاختبار", encoding="utf-8")
    out.write_bytes(b"old" * 500)
    sibling.write_bytes(b"keep-me")

    exhausted = pipeline.TtsEngineExhausted("model-a", "quota")

    async def fake_tts(text, target, engine=""):
        assert text == "نص عربي للاختبار"
        assert target == out
        if engine == "model-a":
            raise exhausted
        target.write_bytes(b"new" * 500)
        return engine

    with mock.patch.object(pipeline, "_gemini_tts_models", return_value=["model-a", "model-b"]), \
            mock.patch.object(pipeline, "tts_one", side_effect=fake_tts) as tts:
        model = asyncio.run(
            pipeline.regenerate_audio_chunk_for_qc(
                src,
                out,
                "gemini",
                pipeline.GEMINI_VOICE,
                {"003.txt": "model-a"},
            )
        )

    assert model == "model-b"
    assert [call.args[2] for call in tts.await_args_list] == ["model-a", "model-b"]
    assert sibling.read_bytes() == b"keep-me"


def test_validate_audio_reuses_passed_chunk_transcript_cache(tmp_path):
    story = tmp_path / "story"
    tts_dir = story / "tts_chunks"
    audio_dir = story / "audio_chunks"
    logs = story / "logs"
    tts_dir.mkdir(parents=True)
    audio_dir.mkdir()
    logs.mkdir()

    source = "هذا نص عربي واضح"
    source_path = tts_dir / "001.txt"
    audio_path = audio_dir / "001.mp3"
    source_path.write_text(source, encoding="utf-8")
    audio_path.write_bytes(b"A" * 4096)
    (story / "12_audio_full.mp3").write_bytes(b"F" * 4096)
    (story / "15_metadata.json").write_text("{}", encoding="utf-8")

    cache = {
        "001.mp3": {
            "status": "passed",
            "audio_sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "similarity": 0.99,
            "length_ratio": 1.0,
        }
    }
    (logs / "audio_qc_chunk_cache.json").write_text(json.dumps(cache), encoding="utf-8")

    fake_run_result = SimpleNamespace(returncode=0, stderr="", stdout="")
    fake_probe = SimpleNamespace(
        stdout=json.dumps({"streams": [{"codec_name": "mp3", "sample_rate": "44100", "channels": 1}]})
    )
    settings = {
        "audio_transcript_qc": True,
        "audio_qc_model": "qc-model",
        "cost_policy": "free_or_preapproved_only",
        "approved_llm_models": ["qc-model"],
        "audio_transcript_similarity_min": 0.82,
        "target_min_seconds": 1000,
        "target_max_seconds": 5000,
    }
    with mock.patch.dict(pipeline.SETTINGS, settings, clear=False), \
            mock.patch.object(pipeline, "run", return_value=fake_run_result), \
            mock.patch.object(pipeline.subprocess, "run", return_value=fake_probe), \
            mock.patch.object(pipeline, "duration", side_effect=[100.0, 3550.0]), \
            mock.patch.object(pipeline, "transcribe_audio_for_qc") as transcribe:
        result = pipeline.validate_audio(story)

    transcribe.assert_not_called()
    assert result["status"] == "PASS"
    assert result["transcript_checks"][0]["cached"] is True


def test_synthesize_repairs_only_the_mismatched_chunk_then_continues(tmp_path):
    story = tmp_path / "story"
    tts_dir = story / "tts_chunks"
    audio_dir = story / "audio_chunks"
    logs = story / "logs"
    tts_dir.mkdir(parents=True)
    audio_dir.mkdir()
    logs.mkdir()
    (tts_dir / "001.txt").write_text("نص القطعة الأولى", encoding="utf-8")

    async def fake_tts(_text, target, _engine=""):
        target.write_bytes(b"A" * 4096)
        return "edge"

    def fake_run(cmd, check=True):
        target = Path(str(cmd[-1]))
        if target.name in {"12_audio_narration.mp3", "13_audio_review.opus"}:
            target.write_bytes(b"M" * 4096)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    def fake_mix(_narration, output):
        output.write_bytes(b"F" * 4096)

    mismatch = pipeline.AudioTranscriptMismatch("001.mp3", 0.50, 1.0)
    settings = {
        "tts_provider": "edge",
        "tts_voice": "voice",
        "tts_rate": "+0%",
        "tts_generation_speed": 1,
        "post_tts_tempo": 1,
        "arabic_diacritization": False,
        "audio_qc_chunk_repair_attempts": 2,
    }

    with mock.patch.dict(pipeline.SETTINGS, settings, clear=False), \
            mock.patch.object(pipeline, "can_reuse_stage", return_value=False), \
            mock.patch.object(pipeline, "prepare"), \
            mock.patch.object(pipeline, "tts_one", side_effect=fake_tts) as tts, \
            mock.patch.object(pipeline, "run", side_effect=fake_run), \
            mock.patch.object(pipeline, "mix_background_audio", side_effect=fake_mix), \
            mock.patch.object(pipeline, "validate_audio", side_effect=[mismatch, {"status": "PASS"}]) as validate, \
            mock.patch.object(pipeline, "duration", return_value=3550.0), \
            mock.patch.object(pipeline, "compute_file_hash", return_value="hash"), \
            mock.patch.object(pipeline, "record_stage"):
        asyncio.run(pipeline.synthesize(story))

    assert tts.await_count == 2
    assert validate.call_count == 2
    assert (audio_dir / "001.mp3").exists()
