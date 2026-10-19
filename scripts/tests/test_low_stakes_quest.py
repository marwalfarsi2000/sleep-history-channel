from __future__ import annotations

import copy
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import quest_rules
from quest_fixtures import candidate, quest_decorate

ROOT = Path(__file__).resolve().parents[2]
PHASES = ["CALM_HOOK"] + ["EXPLORATION"] * 4 + ["SETTLE"] * 2 + ["UNWIND"] * 2 + ["SOFT_FADE"]


def outline() -> list[dict]:
    sections = [{"phase": phase, "scenes": [{}, {}]} for phase in PHASES]
    return quest_decorate(sections)


def calm_story() -> str:
    early = " ".join(["يمشي العطار في السوق ويشم الأعشاب؟ ثم يضع الزجاجة على الطاولة."] * 40)
    middle = " ".join(["يضيف العطار قطرة ويصغي إلى الأصوات البعيدة في الزقاق."] * 80)
    end = " ".join(["يجلس العطار عند الباب وتهدأ الأصوات حوله ببطء."] * 40)
    return f"{early}\n{middle}\n{end}"


class PremiseTests(unittest.TestCase):
    def test_valid_premise_passes(self) -> None:
        self.assertEqual(quest_rules.premise_errors(candidate(1, "a", 4)), [])

    def test_fictional_composite_protagonist_is_allowed(self) -> None:
        item = candidate(1, "a", 4)
        item["protagonist_kind"] = "COMPOSITE_FICTIONAL"
        self.assertEqual(quest_rules.premise_errors(item), [])

    def test_every_flavor_is_accepted(self) -> None:
        for flavor in quest_rules.QUEST_FLAVORS:
            self.assertEqual(quest_rules.premise_errors(candidate(1, "a", 4, flavor)), [], flavor)

    def test_unknown_flavor_fails(self) -> None:
        self.assertTrue(quest_rules.premise_errors(candidate(1, "a", 4, "FACT_LIST")))

    def test_missing_small_want_fails(self) -> None:
        item = candidate(1, "a", 4)
        item["small_want_ar"] = ""
        self.assertTrue(quest_rules.premise_errors(item))

    def test_rejected_markers_fail(self) -> None:
        for phrase in ("من قتل التاجر", "لغز الرسالة", "مؤامرة في القصر", "مطاردة في الأزقة", "اختفاء الصانع", "خيانة الشريك", "انتقام"):
            item = candidate(1, "a", 4)
            item["quest_ar"] = phrase
            self.assertTrue(quest_rules.premise_errors(item), phrase)

    def test_penalty_flags_fail(self) -> None:
        for flag in ("HIGH_STAKES", "MYSTERY_DEPENDENCE", "CLIFFHANGER_DEPENDENCE", "ENCYCLOPEDIC_STRUCTURE"):
            item = candidate(1, "a", 4)
            item["penalty_flags"] = [flag]
            self.assertTrue(quest_rules.premise_errors(item), flag)

    def test_overplotting_fails(self) -> None:
        for field, value in (("main_plots", 3), ("secrets_or_mysteries", 1), ("antagonists", 1), ("named_characters", 9)):
            item = candidate(1, "a", 4)
            item[field] = value
            self.assertTrue(quest_rules.premise_errors(item), field)

    def test_summary_must_be_one_simple_sentence(self) -> None:
        item = candidate(1, "a", 4)
        item["one_sentence_summary_ar"] = "عطار يبحث؟ ثم يجد سرًا. ثم يهرب."
        self.assertTrue(quest_rules.premise_errors(item))


class OutlineTests(unittest.TestCase):
    def test_good_outline_passes(self) -> None:
        self.assertEqual(quest_rules.quest_outline_errors(outline()), [])

    def test_late_new_question_fails(self) -> None:
        sections = outline()
        sections[-2]["new_questions_opened"] = 1
        self.assertTrue(quest_rules.quest_outline_errors(sections))

    def test_no_resolution_before_the_end_fails(self) -> None:
        sections = outline()
        for section in sections:
            if section["quest_state"] == "resolved":
                section["quest_state"] = "in_progress"
        self.assertTrue(quest_rules.quest_outline_errors(sections))

    def test_boring_routine_fails(self) -> None:
        sections = outline()
        for section in sections:
            section["quest_beat"] = "نفس الروتين"
        self.assertTrue(quest_rules.quest_outline_errors(sections))

    def test_repeated_single_place_fails(self) -> None:
        sections = outline()
        for section in sections:
            for scene in section["scenes"]:
                scene["place_or_anchor"] = "الدكان"
        self.assertTrue(quest_rules.quest_outline_errors(sections))

    def test_too_many_named_characters_fails(self) -> None:
        sections = outline()
        sections[3]["named_characters"] = [f"شخص{i}" for i in range(8)]
        self.assertTrue(quest_rules.quest_outline_errors(sections))

    def test_curiosity_must_not_grow_late(self) -> None:
        sections = outline()
        for section in sections[:3]:
            section["new_questions_opened"] = 0
        for section in sections[-4:-2]:
            section["new_questions_opened"] = 3
        self.assertTrue(quest_rules.quest_outline_errors(sections))


class StoryTextTests(unittest.TestCase):
    def test_calm_story_passes(self) -> None:
        self.assertEqual(quest_rules.story_text_errors(calm_story()), [])

    def test_encyclopedic_opening_fails(self) -> None:
        self.assertTrue(quest_rules.story_text_errors("تعتبر مدينة العُلا من أقدم المدن. " + calm_story()))

    def test_cliffhanger_or_thanks_ending_fails(self) -> None:
        for ending in ("شكرًا على الاستماع", "في الحلقة القادمة نكتشف", "لم يكن يعرف ما ينتظره غدًا"):
            self.assertTrue(quest_rules.story_text_errors(calm_story() + " " + ending), ending)

    def test_late_mystery_fails(self) -> None:
        self.assertTrue(quest_rules.story_text_errors(calm_story() + " فجأة ظهر لغز جديد."))

    def test_questions_must_decay(self) -> None:
        busy_end = " ".join(["ماذا سيحدث؟ من هناك؟ أين ذهب؟"] * 40)
        self.assertTrue(quest_rules.story_text_errors(calm_story() + "\n" + busy_end))


class RulesAndDocsConsistencyTests(unittest.TestCase):
    rules = (ROOT / "config" / "master_production_rules.md").read_text(encoding="utf-8")

    def test_identity_and_golden_rule_in_rules(self) -> None:
        self.assertIn(quest_rules.IDENTITY.replace("_", " "), self.rules.upper().replace("-", " "))
        self.assertIn(quest_rules.GOLDEN_RULE, self.rules)

    def test_phases_and_scores_in_rules(self) -> None:
        for phase in ("CALM_HOOK", "EXPLORATION", "SETTLE", "UNWIND", "SOFT_FADE"):
            self.assertIn(phase, self.rules)
        for name in quest_rules.SCORE_NAMES:
            self.assertIn(name.upper(), self.rules.upper())
        for flag in quest_rules.PENALTY_FLAGS:
            self.assertIn(flag, self.rules)

    def test_flavors_and_word_minimums_in_rules(self) -> None:
        for flavor in quest_rules.QUEST_FLAVORS:
            self.assertIn(flavor, self.rules)
        self.assertIn("5000", self.rules)
        self.assertIn("5500", self.rules)

    def test_prompts_use_the_quest_sections(self) -> None:
        source = (ROOT / "scripts" / "generate_daily.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"production_rules\([^)]*65")
        self.assertIn("quest_rules", source)

    def test_story_writing_rules_do_not_ban_fictional_quest_events(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import generate_daily
        sent = generate_daily.production_rules(23, 65)
        for banned in ("- أحداث مخترعة", "أحداث مخترعة.", "ممنوع اختراع الأحداث", "ممنوع اختراع أحداث\n"):
            self.assertNotIn(banned, sent)
        self.assertRegex(sent, r"اختراع أحداث تاريخية وتقديمها كحقائق")
        self.assertIn("PLAUSIBLE FICTION, VERIFIED WORLD", sent)
        self.assertRegex(sent, r"مسموح: أحداث وأفعال الشخصية المركبة الخيالية")

    def test_scores_dimensions_are_ten(self) -> None:
        self.assertEqual(len(quest_rules.SCORE_NAMES), 10)


if __name__ == "__main__":
    unittest.main()
