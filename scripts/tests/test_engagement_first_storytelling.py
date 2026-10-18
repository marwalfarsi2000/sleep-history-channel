from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily
from test_sleep_first_selection import SCORE_NAMES, candidate

ROOT = Path(__file__).resolve().parents[2]
RULES = (ROOT / "config" / "master_production_rules.md").read_text(encoding="utf-8")
GATE = {
    "status": "PASS", "language": True, "religion": True, "sleep": True, "quality": True,
    "universality": True, "non_duplicate": True, "soft_narrative_spine": True,
    "gentle_curiosity": True, "attentional_anchoring": True, "reentry_tolerance": True,
    "gradual_unwind": True, "long_form_without_filler": True, "clear_narrative_thread": True,
    "scene_openable": True, "divisible_into_scenes": True,
    "understandable_without_prior_knowledge": True, "gradually_calmable": True,
    "not_a_fact_list": True, "scores": {name: 5 for name in SCORE_NAMES}, "reasons": [],
}


def scene(n: int = 1) -> dict:
    return {
        "place_or_anchor": f"البوابة {n}", "action": "نقترب", "main_idea": f"فكرة {n}",
        "gentle_question": "من يعبر؟", "transition": "ثم ندخل",
    }


def outline(phases=generate_daily.STORY_PHASES, count: int = 7) -> list[dict]:
    chosen = [phases[min(i * len(phases) // count, len(phases) - 1)] for i in range(count)]
    chosen[0], chosen[-1] = phases[0], phases[-1]
    return [
        {"title": f"قسم {i}", "phase": phase, "brief": "x", "scenes": [scene(1), scene(2)], "target_words": 750}
        for i, phase in enumerate(chosen)
    ]


def run_choose(candidates: list[dict], history: list[dict]) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        bank = Path(directory) / "bank.json"
        bank.write_text("[]", encoding="utf-8")
        with mock.patch.object(generate_daily, "TOPIC_CANDIDATE_BANK_PATH", bank), \
                mock.patch.object(generate_daily, "gemini", side_effect=[
                    (json.dumps({"candidates": candidates}, ensure_ascii=False), []),
                    (json.dumps(GATE), []),
                ]):
            return generate_daily.choose_topic(history)


def six(formats: list[str], scores: list[int]) -> list[dict]:
    return [candidate(i + 1, f"fam{i}", scores[i], formats[i]) for i in range(6)]


class StoryFormatTests(unittest.TestCase):
    def test_formats_include_required_list(self) -> None:
        self.assertEqual(
            set(generate_daily.STORY_FORMATS),
            {"JOURNEY", "DAY_IN_THE_LIFE", "PLACE_IMMERSION", "HOW_IT_WORKED", "EVERYDAY_HABITS", "CLEAR_HISTORICAL_ARC"},
        )

    def test_candidate_without_story_format_is_rejected(self) -> None:
        items = six(["JOURNEY"] * 6, [4] * 6)
        items[2]["story_format"] = ""
        with self.assertRaisesRegex(RuntimeError, "story_format"):
            run_choose(items, [])

    def test_selected_topic_keeps_story_format_and_it_is_persisted(self) -> None:
        formats = ["JOURNEY", "PLACE_IMMERSION", "HOW_IT_WORKED", "EVERYDAY_HABITS", "DAY_IN_THE_LIFE", "CLEAR_HISTORICAL_ARC"]
        topic = run_choose(six(formats, [3, 3, 3, 3, 3, 5]), [])
        self.assertEqual(topic["story_format"], "CLEAR_HISTORICAL_ARC")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.json"
            history.write_text("[]", encoding="utf-8")
            story = root / "stories" / "s"
            story.mkdir(parents=True)
            with mock.patch.object(generate_daily, "ROOT", root):
                generate_daily.upsert_history_checkpoint(history, story, topic, "GENERATING")
            record = json.loads(history.read_text(encoding="utf-8"))[0]
        self.assertEqual(record["story_format"], "CLEAR_HISTORICAL_ARC")

    def test_rotation_never_three_in_a_row(self) -> None:
        history = [{"story_format": "JOURNEY"}, {"story_format": "JOURNEY"}]
        self.assertFalse(generate_daily.story_format_allowed("JOURNEY", history))
        self.assertTrue(generate_daily.story_format_allowed("PLACE_IMMERSION", history))
        self.assertTrue(generate_daily.story_format_allowed("JOURNEY", [{"story_format": "HOW_IT_WORKED"}, {"story_format": "JOURNEY"}]))
        formats = ["JOURNEY", "JOURNEY", "PLACE_IMMERSION", "HOW_IT_WORKED", "EVERYDAY_HABITS", "DAY_IN_THE_LIFE"]
        topic = run_choose(six(formats, [5, 5, 3, 3, 3, 3]), history)
        self.assertNotEqual(topic["story_format"], "JOURNEY")

    def test_variety_breaks_near_ties_but_quality_wins_when_clearly_better(self) -> None:
        history = [{"story_format": "JOURNEY"}, {"story_format": "PLACE_IMMERSION"}]
        near = [dict(candidate(1, "a", 5, "JOURNEY")), dict(candidate(2, "b", 5, "HOW_IT_WORKED"))]
        near[1]["scores"]["novelty_variety"] = 4
        self.assertEqual(generate_daily.pick_topic_by_format(near, history)["story_format"], "HOW_IT_WORKED")
        clear = [candidate(1, "a", 5, "JOURNEY"), candidate(2, "b", 3, "HOW_IT_WORKED")]
        self.assertEqual(generate_daily.pick_topic_by_format(clear, history)["story_format"], "JOURNEY")

    def test_metadata_template_saves_story_format(self) -> None:
        source = (ROOT / "scripts" / "generate_daily.py").read_text(encoding="utf-8")
        self.assertIn('"story_format": topic.get("story_format", "")', source)
        self.assertRegex(source, r'"family", "story_format", "key_elements"')


class OutlineTests(unittest.TestCase):
    def test_outline_not_limited_to_nine_sections(self) -> None:
        for count in (7, 8, 10):
            self.assertEqual(len(generate_daily.validate_outline_sections(outline(count=count))), count)
        for count in (6, 11):
            with self.assertRaises(RuntimeError):
                generate_daily.validate_outline_sections(outline(count=count))

    def test_phases_cover_all_five_in_order(self) -> None:
        sections = outline(count=9)
        self.assertEqual(sections[0]["phase"], "CALM_HOOK")
        self.assertEqual(sections[-1]["phase"], "SOFT_FADE")
        self.assertEqual({s["phase"] for s in sections}, set(generate_daily.STORY_PHASES))
        generate_daily.validate_outline_sections(sections)
        swapped = outline(count=9)
        swapped[0]["phase"], swapped[-1]["phase"] = "SOFT_FADE", "CALM_HOOK"
        with self.assertRaises(RuntimeError):
            generate_daily.validate_outline_sections(swapped)
        missing = outline(count=8)
        for s in missing:
            if s["phase"] == "SETTLE":
                s["phase"] = "EXPLORATION"
        with self.assertRaises(RuntimeError):
            generate_daily.validate_outline_sections(missing)

    def test_every_section_has_scenes_with_required_fields(self) -> None:
        no_scenes = outline()
        no_scenes[3]["scenes"] = []
        with self.assertRaises(RuntimeError):
            generate_daily.validate_outline_sections(no_scenes)
        for field in ("main_idea", "place_or_anchor", "transition"):
            broken = outline()
            broken[2]["scenes"][0][field] = ""
            with self.assertRaises(RuntimeError, msg=field):
                generate_daily.validate_outline_sections(broken)

    def test_make_outline_prompt_and_validation(self) -> None:
        topic = {"topic_ar": "ت", "angle_ar": "ز", "story_format": "JOURNEY"}
        with mock.patch.object(generate_daily, "gemini", return_value=(json.dumps({"sections": outline(count=8)}), [])) as gem:
            result = generate_daily.make_outline(topic, "بحث")
        prompt = gem.call_args.args[0]
        self.assertEqual(len(result), 8)
        for token in ("CALM_HOOK", "SOFT_FADE", "scenes", "main_idea", "transition", "target_words", "من 7 إلى 10"):
            self.assertIn(token, prompt)
        self.assertNotIn("من 9 أقسام", prompt)
        with mock.patch.object(generate_daily, "gemini", return_value=(json.dumps({"sections": [{"title": "x"}] * 9}), [])):
            with self.assertRaises(RuntimeError):
                generate_daily.make_outline(topic, "بحث")

    def test_draft_prompt_uses_scenes_and_keeps_chapter_checkpoints(self) -> None:
        prompts: list[str] = []

        def fake(prompt, **_kw):
            prompts.append(prompt)
            return " ".join(["كلمة"] * 700), []

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(generate_daily, "gemini", side_effect=fake):
            log_dir = Path(directory)
            generate_daily.draft_story({"topic_ar": "ت", "angle_ar": "ز"}, "بحث", outline(count=7), log_dir)
            self.assertEqual(len(list(log_dir.glob("chapter_*.md"))), 7)
        self.assertIn("فكرة 1", prompts[0])
        self.assertIn("المرحلة: CALM_HOOK", prompts[0])
        self.assertIn("بلا عنوان للقسم داخل القصة", prompts[0])
        self.assertIn("لا تخترع شخصية", prompts[0])


class QualityGateTests(unittest.TestCase):
    NEW = (
        "first_listen_comprehension", "teen_first_listen_comprehension", "calm_hook", "clear_narrative_thread",
        "scene_based_storytelling", "one_main_idea_per_scene", "terms_explained", "name_date_density",
        "clear_transitions", "adult_not_childish", "historical_grounding", "no_fictionalized_history",
        "not_boring", "not_overstimulating",
    )

    def passing(self) -> dict:
        base = {name: True for name in (
            "language", "adult_audience", "religion", "sleep", "historical_style", "no_dangerous_drift",
            "soft_narrative_spine", "gentle_curiosity", "attentional_anchoring", "sensory_imagery",
            "low_stakes", "gradual_unwind", "reentry_tolerance", "not_boring_or_overstimulating",
            "flexible_composition", *self.NEW)}
        return {"status": "PASS", **base, "issues": []}

    def run_gate(self, result: dict):
        with mock.patch.object(generate_daily, "gemini", return_value=(json.dumps(result), [])) as gem:
            return generate_daily.validate_story_quality({"topic_ar": "ت", "angle_ar": "ز"}, "نص"), gem

    def test_prompt_lists_all_new_criteria(self) -> None:
        _, gem = self.run_gate(self.passing())
        prompt = gem.call_args.args[0]
        for name in self.NEW:
            self.assertIn(f'"{name}"', prompt)
        self.assertIn("CALM ENGAGEMENT", prompt)

    def test_each_new_criterion_failure_rejects(self) -> None:
        for name in self.NEW:
            result = self.passing()
            result[name] = False
            with self.assertRaisesRegex(RuntimeError, "STORY_QUALITY_GATE_REJECTED", msg=name):
                self.run_gate(result)

    def test_missing_teen_comprehension_rejects(self) -> None:
        result = self.passing()
        del result["teen_first_listen_comprehension"]
        with self.assertRaises(RuntimeError):
            self.run_gate(result)


class RulesTests(unittest.TestCase):
    def test_old_split_removed_and_new_section_present(self) -> None:
        self.assertNotIn("60% الحياة اليومية", RULES)
        self.assertNotIn("30% التاريخ", RULES)
        self.assertNotIn("قرابة 9 أقسام", RULES)
        self.assertIn("لا نسب ثابتة", RULES)
        self.assertIn("# 62. SLEEP-FIRST", RULES)
        self.assertIn("# 64. ENGAGEMENT-FIRST", RULES)

    def test_new_section_content(self) -> None:
        section = generate_daily.production_rules(64)
        for token in ("ENGAGEMENT FIRST", "CALM ENGAGEMENT", "15–16", "الكاميرا السردية", "CALM_HOOK", "SOFT_FADE", "5000", "story_format"):
            self.assertIn(token, section)

    def test_no_prompt_requires_inventing_a_fictional_character(self) -> None:
        rules = generate_daily.production_rules(22, 23, 64)
        self.assertRegex(rules, r"ممنوع اختراع شخصية رئيسية خيالية")
        source = (ROOT / "scripts" / "generate_daily.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(اخترع|أنشئ|ابتكر)\s+(شخصية|بطلًا|بطلا)")

    def test_prompts_include_new_rules(self) -> None:
        source = (ROOT / "scripts" / "generate_daily.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(re.findall(r"62, 64\)", source)), 4)


if __name__ == "__main__":
    unittest.main()
