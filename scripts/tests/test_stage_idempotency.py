import json
import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
import pipeline


def test_background_reuse_existing_image(tmp_path, monkeypatch):
    story_dir = tmp_path / "stories" / "test-story"
    story_dir.mkdir(parents=True)
    img = story_dir / "10_background.png"
    thumb = story_dir / "11_thumbnail_base.jpg"
    img.write_bytes(b"X" * 300_000)
    thumb.write_bytes(b"X" * 10_000)

    prompt_file = story_dir / "08_image_prompt_en.txt"
    prompt_file.write_text("Test image prompt", encoding="utf-8")
    prompt_hash = hashlib.sha256("Test image prompt".encode("utf-8")).hexdigest()

    meta_file = story_dir / "15_metadata.json"
    meta_file.write_text(json.dumps({
        "image_generation_status": "passed",
        "image_prompt_sha256": prompt_hash
    }), encoding="utf-8")

    with patch("pipeline.run") as mock_run:
        pipeline.background(story_dir, force=False)
        mock_run.assert_not_called()


def test_render_reuse_existing_video(tmp_path, monkeypatch):
    story_dir = tmp_path / "stories" / "test-story"
    story_dir.mkdir(parents=True)
    audio = story_dir / "12_audio_full.mp3"
    audio.write_bytes(b"X" * 50_000)
    video = story_dir / "14_video_final.mp4"
    video.write_bytes(b"X" * 1_500_000)

    with patch("pipeline.duration", return_value=120.0), \
         patch("pipeline.background"), \
         patch("pipeline.run") as mock_run:
        pipeline.render(story_dir)
        mock_run.assert_not_called()


def test_completed_artifacts_are_current_skips_rerunning_expensive_stages(tmp_path):
    story_dir = tmp_path / "stories" / "test-story"
    story_dir.mkdir(parents=True)

    files = {
        "05_story_clean.md": b"clean story",
        "05b_story_tts_normalized.md": b"normalized story",
        "07_story_tts_api.md": b"api story",
        "12_audio_full.mp3": b"X" * 5000,
        "10_background.png": b"X" * 5000,
        "11_thumbnail_base.jpg": b"X" * 5000,
        "14_video_final.mp4": b"X" * 5000,
    }
    for name, content in files.items():
        (story_dir / name).write_bytes(content)

    meta = {
        "fact_check_status": "passed",
        "second_fact_check_status": "passed",
        "diacritization_status": "passed",
        "tts_normalization_status": "passed",
        "speech_safe_validation_status": "passed",
        "pronunciation_validation_status": "passed",
        "tts_preparation_status": "passed",
        "audio_qc_status": "passed",
        "image_generation_status": "passed",
        "image_accuracy_status": "passed",
        "video_qc_status": "passed",
        "metadata_validation_status": "passed",
        "clean_story_sha256": hashlib.sha256(b"clean story").hexdigest(),
        "tts_normalized_sha256": hashlib.sha256(b"normalized story").hexdigest(),
        "tts_api_sha256": hashlib.sha256(b"api story").hexdigest(),
        "audio_sha256": hashlib.sha256(b"X" * 5000).hexdigest(),
        "image_sha256": hashlib.sha256(b"X" * 5000).hexdigest(),
        "thumbnail_sha256": hashlib.sha256(b"X" * 5000).hexdigest(),
        "video_sha256": hashlib.sha256(b"X" * 5000).hexdigest(),
    }
    (story_dir / "15_metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    assert pipeline.completed_artifacts_are_current(story_dir) is True


def test_tts_reused_on_retry_after_later_stage_failure(tmp_path, monkeypatch):
    story_dir = tmp_path / "stories" / "test-tts-reuse"
    story_dir.mkdir(parents=True)
    tdir = story_dir / "tts_chunks"
    adir = story_dir / "audio_chunks"
    logs = story_dir / "logs"
    tdir.mkdir()
    adir.mkdir()
    logs.mkdir()

    # Create dummy tts chunk and audio chunk
    chunk_file = tdir / "001.txt"
    chunk_file.write_text("نص تجريبي للاختبار", encoding="utf-8")
    audio_file = adir / "001.mp3"
    audio_file.write_bytes(b"MP3_AUDIO_DATA_" + b"X" * 2000)

    # Calculate expected hash as synthesize does
    import pipeline
    source_hash = hashlib.sha256(
        (
            pipeline.TTS_AUDIO_CACHE_VERSION
            + "\nedge\nedge\nar-AE-FatimaNeural\n"
            + "نص تجريبي للاختبار"
        ).encode("utf-8")
    ).hexdigest()

    (logs / "tts_chunk_hashes.json").write_text(json.dumps({"001.txt": source_hash}), encoding="utf-8")
    (logs / "tts_chunk_models.json").write_text(json.dumps({"001.txt": "edge"}), encoding="utf-8")

    # Mock tts_one so if called it raises an error
    mock_tts_one = MagicMock(side_effect=RuntimeError("TTS generator should NOT be called"))

    with patch("pipeline.SETTINGS", {"tts_provider": "edge", "tts_voice": "ar-AE-FatimaNeural", "background_audio_file": ""}), \
         patch("pipeline.SECRETS", {}), \
         patch("pipeline.prepare"), \
         patch("pipeline.tts_one", mock_tts_one), \
         patch("pipeline.validate_audio"), \
         patch("pipeline.mix_background_audio"), \
             patch("pipeline.duration", return_value=100.0), \
         patch("pipeline.run"):

        import asyncio
        asyncio.run(pipeline.synthesize(story_dir))

        # Prove tts_one was never called and audio chunk remained intact
        mock_tts_one.assert_not_called()
        assert audio_file.exists()
