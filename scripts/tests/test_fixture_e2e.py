import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import state_machine
import error_classifier


def test_mocked_end_to_end_fixture_pipeline(tmp_path):
    story_dir = tmp_path / "stories" / "2026-10-16_test-fixture-story"
    story_dir.mkdir(parents=True)
    logs_dir = story_dir / "logs"
    logs_dir.mkdir()

    # Create dummy source files
    (story_dir / "01_title.txt").write_text("العنوان التجريبي", encoding="utf-8")
    (story_dir / "02_description.txt").write_text("الوصف التجريبي", encoding="utf-8")
    (story_dir / "03_sources.md").write_text("# المصادر", encoding="utf-8")
    (story_dir / "04_research_notes.md").write_text("# الملاحظات", encoding="utf-8")
    (story_dir / "05_story_clean.md").write_text("قصة تاريخية هادئة.", encoding="utf-8")
    (story_dir / "05b_story_tts_normalized.md").write_text("قصة تاريخية هادئة.", encoding="utf-8")
    (story_dir / "06_story_tts_annotated.md").write_text("قِصَّةٌ تَارِيخِيَّةٌ هَادِئَةٌ.", encoding="utf-8")
    (story_dir / "07_story_tts_api.md").write_text("قصة تاريخية هادئة.\n\n", encoding="utf-8")
    (story_dir / "08_image_prompt_en.txt").write_text("historical calm night scene", encoding="utf-8")
    (story_dir / "10_background.png").write_bytes(b"X" * 300_000)
    (story_dir / "11_thumbnail_base.jpg").write_bytes(b"X" * 50_000)
    (story_dir / "12_audio_narration.mp3").write_bytes(b"X" * 100_000)
    (story_dir / "12_audio_full.mp3").write_bytes(b"X" * 100_000)
    (story_dir / "13_audio_review.opus").write_bytes(b"X" * 50_000)
    (story_dir / "14_video_final.mp4").write_bytes(b"X" * 1_500_000)
    (story_dir / "16_fact_check.md").write_text("# Fact check", encoding="utf-8")
    (story_dir / "17_status.md").write_text("# Status", encoding="utf-8")

    meta = {
        "title": "العنوان التجريبي",
        "slug": "test-fixture-story",
        "approval_status": "READY_FOR_APPROVAL",
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
    }
    (story_dir / "15_metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # Verify state machine transition from MEDIA_READY to REVIEW_PENDING
    state = state_machine.normalize_state(meta["approval_status"])
    assert state == state_machine.State.MEDIA_READY

    next_state = state_machine.transition_state(state, state_machine.State.REVIEW_PENDING)
    assert next_state == state_machine.State.REVIEW_PENDING
