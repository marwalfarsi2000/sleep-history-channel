"""Outline robustness: local deterministic repair, all-errors-at-once correction, bounded model calls."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily
import outline_repair
from test_outline_retry import TOPIC, outline, reply, scene

PHASES = generate_daily.STORY_PHASES


def run(responses):
    with mock.patch.object(generate_daily, "gemini", side_effect=responses) as gem:
        return generate_daily.make_outline(TOPIC, "بحث"), gem


def seven(scenes: int = 2, target: int = 750) -> list[dict]:
    sections = outline(7)
    for section in sections:
        section["scenes"] = [scene(i) for i in range(scenes)]
        section["target_words"] = target
    return sections


class LocalRepairTests(unittest.TestCase):
    def test_5250_total_is_raised_locally_with_no_extra_model_call(self) -> None:
        bad = seven(target=750)  # 7 * 750 = 5250, the exact Run 65 failure
        self.assertEqual(sum(s["target_words"] for s in bad), 5250)
        sections, gem = run([reply(bad)])
        self.assertEqual(gem.call_count, 1)
        self.assertGreaterEqual(sum(s["target_words"] for s in sections), 5500)
        self.assertTrue(all(s["target_words"] <= 1400 for s in sections))
        generate_daily.validate_outline_sections(sections)

    def test_minimum_is_never_lowered(self) -> None:
        self.assertEqual(generate_daily.MIN_OUTLINE_TARGET_WORDS, 5500)
        with self.assertRaises(generate_daily.OutlineValidationError):
            generate_daily.validate_outline_sections(seven(target=750))

    def test_string_and_float_target_words_are_coerced(self) -> None:
        bad = seven(target=800)
        bad[0]["target_words"], bad[1]["target_words"], bad[2]["target_words"] = "800", 800.0, "٨٠٠ كلمة"
        sections, gem = run([reply(bad)])
        self.assertEqual(gem.call_count, 1)
        self.assertTrue(all(isinstance(s["target_words"], int) for s in sections))

    def test_six_sections_with_enough_scenes_are_split_to_seven(self) -> None:
        bad = outline(6)
        for section in bad:
            section["scenes"] = [scene(i) for i in range(4)]
            section["target_words"] = 950
        sections, gem = run([reply(bad)])
        self.assertEqual((gem.call_count, len(sections)), (1, 7))
        self.assertTrue(all(2 <= len(s["scenes"]) <= 4 for s in sections))

    def test_eleven_sections_with_same_phase_neighbours_are_merged_to_ten(self) -> None:
        bad = outline(11)
        for section in bad:
            section["target_words"] = 600
        sections, gem = run([reply(bad)])
        self.assertEqual((gem.call_count, len(sections)), (1, 10))
        self.assertTrue(all(len(s["scenes"]) <= 4 for s in sections))

    def test_bad_phase_order_and_missing_phases_are_reassigned_by_position(self) -> None:
        bad = seven(target=800)
        for section in bad:
            section["phase"] = "EXPLORATION"
        sections, gem = run([reply(bad)])
        self.assertEqual(gem.call_count, 1)
        self.assertEqual((sections[0]["phase"], sections[-1]["phase"]), ("CALM_HOOK", "SOFT_FADE"))
        self.assertEqual(set(s["phase"] for s in sections), set(PHASES))

    def test_missing_trivial_scene_fields_are_filled(self) -> None:
        bad = seven(target=800)
        del bad[1]["scenes"][0]["transition"]
        del bad[2]["scenes"][0]["main_idea"]
        sections, gem = run([reply(bad)])
        self.assertEqual(gem.call_count, 1)
        self.assertEqual(sections[1]["scenes"][0]["transition"], outline_repair.NEUTRAL_TRANSITION)
        self.assertEqual(sections[2]["scenes"][0]["main_idea"], bad[2]["scenes"][0]["action"])

    def test_repair_never_invents_historical_content(self) -> None:
        bad = seven(target=750)
        original = json.dumps(bad, ensure_ascii=False)
        sections, _ = run([reply(bad)])
        allowed = set(original.replace('"', " ").split()) | set(outline_repair.NEUTRAL_TRANSITION.split())
        for section in sections:
            for key in ("title", "brief"):
                self.assertTrue(set(str(section[key]).split()) <= allowed)
            for item in section["scenes"]:
                for value in item.values():
                    self.assertTrue(set(str(value).split()) <= allowed)

    def test_unrepairable_input_is_left_for_the_validator(self) -> None:
        for raw in (None, [], "x", [1, 2], {"a": 1}):
            self.assertEqual(outline_repair.repair_outline(
                raw, phases=PHASES, min_sections=7, max_sections=10, min_scenes=2, max_scenes=4,
                min_total_words=5500)[0], raw)

    def test_repair_is_deterministic_and_does_not_mutate_the_input(self) -> None:
        bad = seven(target=750)
        snapshot = copy.deepcopy(bad)
        first = outline_repair.repair_outline(bad, phases=PHASES, min_sections=7, max_sections=10,
                                              min_scenes=2, max_scenes=4, min_total_words=5500)
        second = outline_repair.repair_outline(bad, phases=PHASES, min_sections=7, max_sections=10,
                                               min_scenes=2, max_scenes=4, min_total_words=5500)
        self.assertEqual(first, second)
        self.assertEqual(bad, snapshot)


class AdaptiveRetryTests(unittest.TestCase):
    def test_one_model_correction_then_local_repair_ends_in_success(self) -> None:
        a1 = seven()
        a1[2]["scenes"] = [scene()]          # first response needs a model correction
        a2 = seven(target=750)               # corrected structure; 5250 is raised locally
        sections, gem = run([reply(a1), reply(a2)])
        self.assertEqual(gem.call_count, 2)
        generate_daily.validate_outline_sections(sections)

    def test_all_errors_are_sent_to_the_model_in_one_correction(self) -> None:
        bad = outline(6)
        bad[1]["scenes"] = [scene()]
        bad[0]["target_words"] = 100
        _, gem = run([reply(bad), reply(seven(target=900))])
        second = gem.call_args_list[1].args[0]
        self.assertIn("VALIDATION_ERRORS", second)
        self.assertIn("macro sections", second)
        self.assertIn("needs 2-4 scenes", second)

    def test_correction_prompt_carries_a_compact_summary_not_the_raw_outline(self) -> None:
        bad = outline(6)
        bad[1]["scenes"] = [scene()]
        bad[3]["brief"] = "نص-خام-طويل-لا-يجب-إعادة-إرساله" * 20
        _, gem = run([reply(bad), reply(seven(target=900))])
        second = gem.call_args_list[1].args[0]
        self.assertNotIn("نص-خام-طويل", second)
        self.assertLess(len(second) - len(gem.call_args_list[0].args[0]), 2500)

    def test_model_calls_are_bounded(self) -> None:
        bad = outline(6)
        bad[1]["scenes"] = [scene()]
        with self.assertRaisesRegex(RuntimeError, "OUTLINE_GENERATION_FAILED_AFTER_RETRIES"):
            run([reply(bad)] * 4)
        with mock.patch.object(generate_daily, "gemini", side_effect=[reply(bad)] * 10) as gem:
            with self.assertRaises(RuntimeError):
                generate_daily.make_outline(TOPIC, "بحث")
        self.assertEqual(gem.call_count, generate_daily.OUTLINE_MAX_MODEL_CALLS)


if __name__ == "__main__":
    unittest.main()
