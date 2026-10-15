from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_daily

TOPIC = {"topic_ar": "قرطبة", "angle_ar": "الحياة اليومية"}
SECTION = [{"title": "السوق", "brief": "صباح السوق", "target_words": 850}]


def words(count: int) -> str:
    return " ".join(["كلمة"] * count)


class ChapterLengthTests(unittest.TestCase):
    def test_short_chapter_is_extended_by_continuation_when_rewrite_does_not_grow(self):
        # Draft 300 words, rewrite comes back shorter (ignored), continuation adds 350.
        responses = [(words(300), []), (words(290), []), (words(350), [])]
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(generate_daily, "production_rules", return_value=""), \
             mock.patch.object(generate_daily, "gemini", side_effect=responses) as gemini:
            story = generate_daily.draft_story(TOPIC, "research", SECTION, Path(folder))
            saved = (Path(folder) / "chapter_01.md").read_text(encoding="utf-8")
        self.assertEqual(generate_daily.word_count(story), 650)
        self.assertEqual(saved.strip(), story.strip())
        self.assertEqual(gemini.call_count, 3)

    def test_chapter_that_never_reaches_minimum_still_fails_closed(self):
        responses = [(words(100), [])] + [(words(50), [])] * 4
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(generate_daily, "production_rules", return_value=""), \
             mock.patch.object(generate_daily, "gemini", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "too short"):
                generate_daily.draft_story(TOPIC, "research", SECTION, Path(folder))


if __name__ == "__main__":
    unittest.main()
