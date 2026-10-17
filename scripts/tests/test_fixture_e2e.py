import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import state_machine
import cloud_pipeline


def test_deterministic_mocked_orchestration_e2e(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_pipeline, "ROOT", tmp_path)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "settings.json").write_text(json.dumps({"background_audio_file": "assets/audio/pink-noise.m4a"}), encoding="utf-8")
    (tmp_path / "config" / "topics_history.json").write_text(json.dumps([]), encoding="utf-8")
    (tmp_path / "config" / "archive_index.json").write_text(json.dumps([]), encoding="utf-8")

    story_dir = tmp_path / "stories" / "2026-10-16_e2e-story"
    story_dir.mkdir(parents=True)
    (story_dir / "logs").mkdir()

    # Pre-populate story files as if generated
    (story_dir / "01_title.txt").write_text("عنوان القصة", encoding="utf-8")
    (story_dir / "02_description.txt").write_text("وصف القصة", encoding="utf-8")
    (story_dir / "03_sources.md").write_text("المصادر", encoding="utf-8")
    (story_dir / "04_research_notes.md").write_text("الملاحظات", encoding="utf-8")
    (story_dir / "05_story_clean.md").write_text("نص القصة", encoding="utf-8")
    (story_dir / "05b_story_tts_normalized.md").write_text("نص القصة", encoding="utf-8")
    (story_dir / "06_story_tts_annotated.md").write_text("نص القصة المشكول", encoding="utf-8")
    (story_dir / "07_story_tts_api.md").write_text("نص القصة النهائي", encoding="utf-8")
    (story_dir / "08_image_prompt_en.txt").write_text("prompt", encoding="utf-8")
    (story_dir / "10_background.png").write_bytes(b"X" * 300_000)
    (story_dir / "11_thumbnail_base.jpg").write_bytes(b"X" * 50_000)
    (story_dir / "12_audio_narration.mp3").write_bytes(b"X" * 100_000)
    (story_dir / "12_audio_full.mp3").write_bytes(b"X" * 100_000)
    (story_dir / "13_audio_review.opus").write_bytes(b"X" * 50_000)
    (story_dir / "14_video_final.mp4").write_bytes(b"X" * 1_500_000)
    (story_dir / "16_fact_check.md").write_text("fact check", encoding="utf-8")
    (story_dir / "17_status.md").write_text("status", encoding="utf-8")

    meta = {
        "title": "عنوان القصة",
        "slug": "e2e-story",
        "approval_status": state_machine.State.MEDIA_READY,
        "video_duration_seconds": 120.0
    }
    (story_dir / "15_metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    fake_archive_record = {
        "identifier": "lan-tukmil-alqissa-20261016-e2e-story",
        "item_url": "https://archive.org/details/lan-tukmil-alqissa-20261016-e2e-story",
    }

    fake_yt_result = {
        "video_id": "yt_e2e_123",
        "url": "https://www.youtube.com/watch?v=yt_e2e_123",
    }

    # Mock all external network calls (Internet Archive, Telegram, YouTube)
    fake_archive_metadata = {
        "metadata": {
            "title": "عنوان القصة",
            "approval_status": state_machine.State.REVIEW_PENDING,
            "youtube_packaging_status": None,
        }
    }

    with patch("cloud_pipeline.ensure_background_audio"), \
         patch("cloud_pipeline.run", return_value=json.dumps({"story_path": str(story_dir)})), \
         patch("cloud_pipeline.upload_story", return_value=fake_archive_record), \
         patch("cloud_pipeline.notify_review"), \
         patch("cloud_pipeline.modify_remote"), \
         patch("cloud_pipeline.set_index_status"), \
         patch("cloud_pipeline.advance_index_status"), \
         patch("cloud_pipeline.send_message"), \
         patch("cloud_pipeline.archive_metadata", return_value=fake_archive_metadata), \
         patch("archive_recovery.ensure_index_record"), \
         patch("youtube_publish.publish_archive_item", return_value=fake_yt_result), \
         patch.dict("os.environ", {"IAS3_ACCESS_KEY": "acc", "IAS3_SECRET_KEY": "sec", "TELEGRAM_CHAT_ID": "123"}):

        # 1. Produce
        prod_res = cloud_pipeline.produce()
        assert prod_res["status"] == state_machine.State.REVIEW_PENDING

        # 2. Review
        with patch("cloud_pipeline.remote_status", return_value=state_machine.State.REVIEW_PENDING):
            pub_res = cloud_pipeline.publish("lan-tukmil-alqissa-20261016-e2e-story")
            assert pub_res["video_id"] == "yt_e2e_123"
