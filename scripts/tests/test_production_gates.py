from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_daily
import pipeline
import cloud_pipeline


class ProductionGateTests(unittest.TestCase):
    def test_research_rejects_too_few_grounded_sources(self):
        with mock.patch.object(
            generate_daily,
            "gemini",
            return_value=("خريطة الادعاءات\nhttps://a.example/x", [{"title": "A", "url": "https://a.example/x"}]),
        ):
            with self.assertRaisesRegex(RuntimeError, "SOURCE_VALIDATION_FAILED"):
                generate_daily.research_topic({
                    "topic_ar": "موضوع", "angle_ar": "زاوية", "period_ar": "فترة"
                })

    def test_fact_check_applies_corrections_and_requires_second_pass(self):
        original = " ".join(["قصة"] * 5000)
        corrected = " ".join(["حكاية"] + ["قصة"] * 4999)
        responses = [
            (json.dumps({
                "status": "NEEDS_CORRECTION", "verified": [], "uncertain": [],
                "corrected": [{"original": "قصة", "replacement": "حكاية", "source": "https://source.example"}],
                "unsupported_critical": [],
            }), []),
            (corrected, []),
            (json.dumps({
                "status": "PASS", "remaining_errors": [], "unsupported_critical": [],
                "uncertain_presented_as_fact": [], "corrections_applied": True,
            }), []),
        ]
        with mock.patch.object(generate_daily, "gemini", side_effect=responses):
            final, report = generate_daily.fact_check_story(
                {"topic_ar": "موضوع"}, "بحث موثق", original
            )
        self.assertEqual(final, corrected)
        self.assertEqual(report["status"], "PASS")

    def test_tts_prepare_rejects_missing_upstream_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            story = Path(directory)
            (story / "15_metadata.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "TTS_HARD_GATE_FAILED"):
                pipeline.prepare(story)

    def test_tts_chunking_refuses_to_cut_an_oversized_sentence(self):
        with self.assertRaisesRegex(RuntimeError, "TTS_CHUNKING_FAILED"):
            pipeline.split_chunks("ا" * 101 + ".", limit=100)

    def test_production_uploads_review_copy_to_archive_as_review_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "stories" / "episode"
            story.mkdir(parents=True)
            (root / "config").mkdir()
            (root / "config" / "topics_history.json").write_text(
                json.dumps([{"slug": "episode", "status": "drafting", "story_path": "stories/episode"}]),
                encoding="utf-8",
            )
            (story / "15_metadata.json").write_text(json.dumps({
                "slug": "episode", "title": "Episode", "approval_status": "READY_FOR_APPROVAL",
                "video_duration_seconds": 3600,
            }), encoding="utf-8")
            generated = json.dumps({"story_path": str(story), "slug": "episode"})
            with mock.patch.object(cloud_pipeline, "ROOT", root), \
                 mock.patch.object(cloud_pipeline, "ensure_background_audio"), \
                 mock.patch.object(cloud_pipeline, "run", side_effect=[generated, ""]), \
                 mock.patch.object(cloud_pipeline, "upload_story", return_value={
                     "identifier": "lan-tukmil-alqissa-episode",
                     "item_url": "https://archive.org/details/lan-tukmil-alqissa-episode",
                 }) as upload:
                result = cloud_pipeline.produce()
            upload.assert_called_once()
            self.assertEqual(result["status"], "REVIEW_PENDING")
            self.assertEqual(result["identifier"], "lan-tukmil-alqissa-episode")
            self.assertEqual(result["review_url"], "https://archive.org/details/lan-tukmil-alqissa-episode")

    def test_reject_deletes_only_review_pending_items(self):
        with mock.patch.object(cloud_pipeline, "remote_status", return_value="COMPLETED"), \
             mock.patch.object(cloud_pipeline, "delete_rejected") as delete:
            with self.assertRaises(RuntimeError):
                cloud_pipeline.reject("published-item")
        delete.assert_not_called()

    def test_publish_requires_review_pending(self):
        with mock.patch.object(cloud_pipeline, "remote_status", return_value="REJECTED_DELETED"):
            with self.assertRaisesRegex(RuntimeError, "HUMAN_APPROVAL_GATE_FAILED"):
                cloud_pipeline.publish("item")


if __name__ == "__main__":
    unittest.main()
