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
            self.assertEqual(history[0]["status"], "generating")
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


if __name__ == "__main__":
    unittest.main()
