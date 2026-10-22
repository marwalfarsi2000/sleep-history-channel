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
        self.assertEqual(generate_daily.chapter_preferred_words(850), 595)
        self.assertEqual(generate_daily.chapter_preferred_words(700), 550)
        self.assertEqual(generate_daily.chapter_preferred_words(1000), 700)
        self.assertEqual(generate_daily.chapter_minimum_words(850), 595)

    def test_short_fresh_chapter_is_extended_by_continuation_only(self):
        with tempfile.TemporaryDirectory() as folder:
            story, gemini = self.draft([(words(300), []), (words(500), [])], folder)
            saved = (Path(folder) / "chapter_01.md").read_text(encoding="utf-8")
        self.assertEqual(generate_daily.word_count(story), 800)
        self.assertEqual(saved.strip(), story.strip())
        self.assertEqual(gemini.call_count, 2)  # draft + one continuation, no rewrite

    def test_600_and_700_word_chapters_are_accepted_without_forced_expansion(self):
        for count in (600, 700):
            with tempfile.TemporaryDirectory() as folder:
                chapter = Path(folder) / "chapter_01.md"
                chapter.write_text(words(count), encoding="utf-8")
                story, gemini = self.draft([], folder)
            self.assertEqual(gemini.call_count, 0, count)
            self.assertEqual(generate_daily.word_count(story), count)

    def test_existing_short_chapter_is_appended_not_regenerated(self):
        with tempfile.TemporaryDirectory() as folder:
            chapter = Path(folder) / "chapter_01.md"
            original = words(500)
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
            (Path(folder) / "chapter_01.md").write_text(words(595), encoding="utf-8")
            story, gemini = self.draft([], folder)
        self.assertEqual(gemini.call_count, 0)
        self.assertEqual(generate_daily.word_count(story), 595)

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
            (Path(folder) / "chapter_01.md").write_text(words(580), encoding="utf-8")
            story, gemini = self.draft([(words(250), [])], folder)
        self.assertEqual(gemini.call_count, 1)
        self.assertGreaterEqual(generate_daily.word_count(story), 595)
        self.assertIn("بنحو 350 كلمة عربية", gemini.call_args.args[0])

    def test_short_chapter_after_one_continuation_is_soft_warning_not_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "chapter_01.md").write_text(words(409), encoding="utf-8")
            with mock.patch("sys.stderr") as err:
                story, gemini = self.draft([(words(5), [])], folder)
        self.assertEqual(gemini.call_count, 1)
        self.assertEqual(generate_daily.word_count(story), 414)
        written = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("CHAPTER_LENGTH_SOFT_WARNING", written)
        self.assertIn("hard 5000-word gate", written)

    def test_per_chapter_length_is_not_a_hard_failure_anymore(self):
        import inspect
        source = inspect.getsource(generate_daily.draft_story)
        self.assertNotIn("CHAPTER_LENGTH_TARGET_FAILED", source)
        self.assertIn("CHAPTER_LENGTH_SOFT_WARNING", source)


class StoryMinimumRepairTests(unittest.TestCase):
    def test_story_just_under_5000_gets_one_append_only_topup_in_penultimate_chapter(self):
        sections = [
            {"title": f"قسم {i}", "brief": "هادئ", "phase": "EXPLORATION", "scenes": []}
            for i in range(1, 9)
        ]
        with tempfile.TemporaryDirectory() as folder:
            logs = Path(folder)
            counts = [625, 625, 625, 625, 625, 625, 601, 625]  # total 4976
            for i, count in enumerate(counts, 1):
                (logs / f"chapter_{i:02d}.md").write_text(words(count), encoding="utf-8")
            draft = "\n\n".join((logs / f"chapter_{i:02d}.md").read_text(encoding="utf-8") for i in range(1, 9))
            original_seventh = (logs / "chapter_07.md").read_text(encoding="utf-8")
            original_eighth = (logs / "chapter_08.md").read_text(encoding="utf-8")
            with mock.patch.object(generate_daily, "gemini", return_value=(words(250), [])) as gemini:
                repaired = generate_daily.ensure_minimum_story_length(
                    {"topic_ar": "مسقط"}, "بحث", sections, logs, draft
                )

            self.assertEqual(gemini.call_count, 1)
            self.assertGreaterEqual(generate_daily.word_count(repaired), 5000)
            self.assertTrue((logs / "chapter_07.md").read_text(encoding="utf-8").startswith(original_seventh))
            self.assertEqual((logs / "chapter_08.md").read_text(encoding="utf-8"), original_eighth)
            self.assertIn("محاولة إصلاح واحدة", gemini.call_args.args[0])
            self.assertIn("لا تفتح لغزًا أو سؤالًا جديدًا", gemini.call_args.args[0])

    def test_story_at_or_over_5000_never_calls_model(self):
        sections = [{"title": "قسم", "brief": "هادئ", "phase": "UNWIND", "scenes": []}]
        with tempfile.TemporaryDirectory() as folder:
            logs = Path(folder)
            draft = words(5000)
            with mock.patch.object(generate_daily, "gemini") as gemini:
                repaired = generate_daily.ensure_minimum_story_length(
                    {"topic_ar": "مسقط"}, "بحث", sections, logs, draft
                )
        self.assertEqual(repaired, draft)
        gemini.assert_not_called()

    def test_failed_single_topup_remains_fail_closed_below_5000(self):
        sections = [
            {"title": f"قسم {i}", "brief": "هادئ", "phase": "UNWIND", "scenes": []}
            for i in range(1, 3)
        ]
        with tempfile.TemporaryDirectory() as folder:
            logs = Path(folder)
            (logs / "chapter_01.md").write_text(words(2200), encoding="utf-8")
            (logs / "chapter_02.md").write_text(words(2200), encoding="utf-8")
            draft = words(4400)
            with mock.patch.object(generate_daily, "gemini", return_value=(words(10), [])):
                repaired = generate_daily.ensure_minimum_story_length(
                    {"topic_ar": "مسقط"}, "بحث", sections, logs, draft
                )
        self.assertLess(generate_daily.word_count(repaired), 5000)

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

    def test_final_story_gate_still_5000_after_one_local_topup(self):
        import inspect
        source = inspect.getsource(generate_daily.resume_partial_story)
        self.assertIn("ensure_minimum_story_length", source)
        self.assertIn("word_count(draft) < 5000", source)
        self.assertIn("STORY_TOO_SHORT", source)
        self.assertEqual(generate_daily.MIN_OUTLINE_TARGET_WORDS, 5500)


if __name__ == "__main__":
    unittest.main()
