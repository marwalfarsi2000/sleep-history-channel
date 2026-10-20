from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_daily

TOPIC = {"topic_ar": "قرطبة", "angle_ar": "الحياة اليومية"}
SECTION = [{"title": "السوق", "brief": "صباح السوق", "target_words": 850}]


def words(count: int) -> str:
    return " ".join(["كلمة"] * count)


class ChapterLengthTests(unittest.TestCase):
    def draft(self, responses, folder, section=SECTION):
        with mock.patch.object(generate_daily, "production_rules", return_value=""), \
             mock.patch.object(generate_daily, "gemini", side_effect=responses) as gemini:
            try:
                return generate_daily.draft_story(TOPIC, "research", section, Path(folder)), gemini
            except RuntimeError as exc:
                exc.gemini = gemini
                raise

    def test_minimum_formula(self):
        self.assertEqual(generate_daily.chapter_minimum_words(850), 765)
        self.assertEqual(generate_daily.chapter_minimum_words(700), 750)
        self.assertEqual(generate_daily.chapter_minimum_words(1000), 900)

    def test_short_fresh_chapter_is_extended_by_continuation_only(self):
        with tempfile.TemporaryDirectory() as folder:
            story, gemini = self.draft([(words(300), []), (words(500), [])], folder)
            saved = (Path(folder) / "chapter_01.md").read_text(encoding="utf-8")
        self.assertEqual(generate_daily.word_count(story), 800)
        self.assertEqual(saved.strip(), story.strip())
        self.assertEqual(gemini.call_count, 2)  # draft + one continuation, no rewrite

    def test_600_and_700_word_chapters_still_need_expansion(self):
        for count in (600, 700):
            with tempfile.TemporaryDirectory() as folder:
                chapter = Path(folder) / "chapter_01.md"
                chapter.write_text(words(count), encoding="utf-8")
                story, gemini = self.draft([(words(200), [])], folder)
            self.assertEqual(gemini.call_count, 1, count)
            self.assertEqual(generate_daily.word_count(story), count + 200)

    def test_existing_short_chapter_is_appended_not_regenerated(self):
        with tempfile.TemporaryDirectory() as folder:
            chapter = Path(folder) / "chapter_01.md"
            original = words(600)
            chapter.write_text(original, encoding="utf-8")
            story, gemini = self.draft([("إضافة " * 200, [])], folder)
            saved = chapter.read_text(encoding="utf-8")
        self.assertTrue(saved.startswith(original))
        self.assertEqual(gemini.call_count, 1)
        prompt = gemini.call_args.args[0]
        self.assertIn("أكمل من نهاية النص مباشرة", prompt)
        self.assertIn("دون حشو", prompt)
        self.assertIn("لا تلخص", prompt)

    def test_sufficient_existing_chapter_is_reused_without_model_call(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "chapter_01.md").write_text(words(765), encoding="utf-8")
            story, gemini = self.draft([], folder)
        self.assertEqual(gemini.call_count, 0)
        self.assertEqual(generate_daily.word_count(story), 765)

    def test_continuation_prompt_keeps_phase_and_scenes(self):
        section = [{**SECTION[0], "phase": "UNWIND", "scenes": [{"main_idea": "فكرة خاصة"}]}]
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "chapter_01.md").write_text(words(400), encoding="utf-8")
            _, gemini = self.draft([(words(400), [])], folder, section)
        prompt = gemini.call_args.args[0]
        self.assertIn("UNWIND", prompt)
        self.assertIn("فكرة خاصة", prompt)

    def test_small_deficit_requests_a_real_continuation_instead_of_tiny_patch(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "chapter_01.md").write_text(words(748), encoding="utf-8")
            story, gemini = self.draft([(words(250), [])], folder)
        self.assertEqual(gemini.call_count, 1)
        self.assertGreaterEqual(generate_daily.word_count(story), 765)
        self.assertIn("بنحو 350 كلمة عربية", gemini.call_args.args[0])

    def test_chapter_that_never_reaches_minimum_fails_closed(self):
        final_count = 702 + (5 * generate_daily.CHAPTER_EXPANSION_ATTEMPTS)
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "chapter_01.md").write_text(words(702), encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError,
                rf"CHAPTER_LENGTH_TARGET_FAILED: chapter 1 has {final_count} words; minimum acceptable is 765",
            ) as ctx:
                self.draft([(words(5), [])] * generate_daily.CHAPTER_EXPANSION_ATTEMPTS, folder)
        self.assertEqual(ctx.exception.gemini.call_count, generate_daily.CHAPTER_EXPANSION_ATTEMPTS)

    def test_old_600_threshold_is_gone(self):
        import inspect
        self.assertNotIn("< 600", inspect.getsource(generate_daily.draft_story))


class OutlineTotalTests(unittest.TestCase):
    @staticmethod
    def sections(per_section):
        import test_outline_retry as helper
        result = helper.outline(7)
        for section in result:
            section["target_words"] = per_section
        return result

    def test_total_at_minimum_passes(self):
        sections = self.sections(800)
        sections[0]["target_words"] = 5500 - 800 * 6
        self.assertEqual(sum(s["target_words"] for s in sections), 5500)
        generate_daily.validate_outline_sections(sections)

    def test_total_below_minimum_rejected(self):
        with self.assertRaisesRegex(generate_daily.OutlineValidationError, "total is 4900; minimum is 5500"):
            generate_daily.validate_outline_sections(self.sections(700))

    def test_non_numeric_target_rejected(self):
        sections = self.sections(800)
        sections[2]["target_words"] = "كثير"
        with self.assertRaises(generate_daily.OutlineValidationError):
            generate_daily.validate_outline_sections(sections)

    def test_low_total_is_repaired_locally_without_a_second_model_call(self):
        import json as js
        low = self.sections(700)
        with mock.patch.object(generate_daily, "gemini", side_effect=[(js.dumps({"sections": low}), [])]) as gemini:
            result = generate_daily.make_outline({"topic_ar": "ت", "angle_ar": "ز"}, "بحث")
        self.assertEqual(gemini.call_count, 1)
        self.assertGreaterEqual(sum(s["target_words"] for s in result), 5500)
        generate_daily.validate_outline_sections(result)

    def test_final_story_gate_still_5000(self):
        import inspect
        source = inspect.getsource(generate_daily.resume_partial_story)
        self.assertIn("word_count(draft) < 5000", source)
        self.assertEqual(generate_daily.MIN_OUTLINE_TARGET_WORDS, 5500)


if __name__ == "__main__":
    unittest.main()
