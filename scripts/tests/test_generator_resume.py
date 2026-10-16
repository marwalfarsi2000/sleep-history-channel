from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_daily


class GeneratorResumeTests(unittest.TestCase):
    def test_failure_keeps_generating_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "stories").mkdir()
            (root / "config" / "topics_history.json").write_text("[]\n", encoding="utf-8")
            topic = {
                "slug": "checkpoint-topic",
                "topic_ar": "موضوع",
                "angle_ar": "زاوية",
                "search_query_en": "topic",
                "period_ar": "فترة",
            }
            with mock.patch.object(generate_daily, "ROOT", root), \
                 mock.patch.object(generate_daily, "choose_topic", return_value=topic), \
                 mock.patch.object(generate_daily, "research_topic", side_effect=RuntimeError("temporary")):
                with self.assertRaisesRegex(RuntimeError, "temporary"):
                    generate_daily.create_daily_story()
            history = json.loads((root / "config" / "topics_history.json").read_text(encoding="utf-8"))
            from state_machine import normalize_state, State
            self.assertEqual(normalize_state(history[0]["status"]), State.GENERATING)
            self.assertTrue((root / history[0]["story_path"]).exists())

    def test_existing_generating_checkpoint_is_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "stories" / "partial"
            story.mkdir(parents=True)
            (root / "config").mkdir()
            history = [{"slug": "partial", "status": "generating", "story_path": "stories/partial"}]
            (root / "config" / "topics_history.json").write_text(
                json.dumps(history), encoding="utf-8"
            )
            with mock.patch.object(generate_daily, "ROOT", root), \
                 mock.patch.object(generate_daily, "resume_partial_story", return_value=story) as resume, \
                 mock.patch.object(generate_daily, "choose_topic") as choose:
                result = generate_daily.create_daily_story()
            self.assertEqual(result, story)
            resume.assert_called_once_with(story.resolve())
            choose.assert_not_called()

    def test_pending_approval_without_archive_is_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "stories" / "upload-failed"
            story.mkdir(parents=True)
            (root / "config").mkdir()
            history = [{
                "slug": "upload-failed",
                "status": "pending_approval",
                "story_path": "stories/upload-failed",
            }]
            (root / "config" / "topics_history.json").write_text(
                json.dumps(history), encoding="utf-8"
            )
            with mock.patch.object(generate_daily, "ROOT", root), \
                 mock.patch.object(generate_daily, "resume_partial_story", return_value=story) as resume, \
                 mock.patch.object(generate_daily, "choose_topic") as choose:
                result = generate_daily.create_daily_story()
            self.assertEqual(result, story)
            resume.assert_called_once_with(story.resolve())
            choose.assert_not_called()

    def test_latest_missing_draft_rebuilds_same_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "stories").mkdir()
            history = [
                {
                    "slug": "older-draft",
                    "topic": "موضوع أقدم",
                    "angle": "زاوية أقدم",
                    "status": "drafting",
                    "story_path": "stories/older-draft",
                },
                {
                    "slug": "latest-draft",
                    "topic": "موضوع أخير",
                    "angle": "زاوية أخيرة",
                    "status": "drafting",
                    "story_path": "stories/latest-draft",
                },
            ]
            (root / "config" / "topics_history.json").write_text(
                json.dumps(history, ensure_ascii=False), encoding="utf-8"
            )
            with mock.patch.object(generate_daily, "ROOT", root), \
                 mock.patch.object(generate_daily, "choose_topic") as choose, \
                 mock.patch.object(generate_daily, "research_topic", side_effect=RuntimeError("stop after checkpoint")):
                with self.assertRaisesRegex(RuntimeError, "stop after checkpoint"):
                    generate_daily.create_daily_story()
            latest = root / "stories" / "latest-draft"
            self.assertTrue(latest.exists())
            saved_topic = json.loads((latest / "logs" / "topic.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_topic["slug"], "latest-draft")
            self.assertEqual(saved_topic["topic_ar"], "موضوع أخير")
            choose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
