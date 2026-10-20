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
from quest_fixtures import SCORE_NAMES, candidate, gate, quest_decorate

ROOT = Path(__file__).resolve().parents[2]
RULES = (ROOT / "config" / "master_production_rules.md").read_text(encoding="utf-8")
GATE = gate()


def scene(n: int = 1) -> dict:
    return {
        "place_or_anchor": f"البوابة {n}", "action": "نقترب", "main_idea": f"فكرة {n}",
        "gentle_question": "من يعبر؟", "transition": "ثم ندخل",
    }


def outline(phases=generate_daily.STORY_PHASES, count: int = 7) -> list[dict]:
    chosen = [phases[min(i * len(phases) // count, len(phases) - 1)] for i in range(count)]
    chosen[0], chosen[-1] = phases[0], phases[-1]
    return quest_decorate([
        {"title": f"قسم {i}", "phase": phase, "brief": "x", "scenes": [scene(1), scene(2)], "target_words": 800}
        for i, phase in enumerate(chosen)
    ])


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
            {"CRAFT_QUEST", "MARKET_QUEST", "JOURNEY_QUEST", "PREPARATION_QUEST", "SEARCH_FOR_MATERIAL",
             "MAKE_OR_REPAIR", "TRADE_OR_DELIVERY", "HOSTING_OR_ARRIVAL", "SEASONAL_TASK", "WORKSHOP_QUEST"},
        )
        self.assertFalse(set(generate_daily.STORY_FORMATS) & set(generate_daily.quest_rules.LEGACY_FORMATS))

    def test_candidate_without_story_format_is_excluded_not_fatal(self) -> None:
        items = six(["JOURNEY_QUEST"] * 6, [4, 4, 4, 4, 4, 5])
        items[5]["story_format"] = ""
        self.assertNotEqual(run_choose(items, [])["slug"], "topic-6")
        for item in items[:3]:
            item["story_format"] = ""
        with self.assertRaisesRegex(RuntimeError, "TOPIC_DISCOVERY_FAILED"):
            run_choose(items, [])

    def test_selected_topic_keeps_story_format_and_it_is_persisted(self) -> None:
        formats = ["JOURNEY_QUEST", "MARKET_QUEST", "CRAFT_QUEST", "SEASONAL_TASK", "WORKSHOP_QUEST", "HOSTING_OR_ARRIVAL"]
        topic = run_choose(six(formats, [3, 3, 3, 3, 3, 5]), [])
        self.assertEqual(topic["story_format"], "HOSTING_OR_ARRIVAL")
        self.assertEqual(topic["narrative_identity"], "IMMERSIVE_LOW_STAKES_HISTORICAL_QUEST")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.json"
            history.write_text("[]", encoding="utf-8")
            story = root / "stories" / "s"
            story.mkdir(parents=True)
            with mock.patch.object(generate_daily, "ROOT", root):
                generate_daily.upsert_history_checkpoint(history, story, topic, "GENERATING")
            record = json.loads(history.read_text(encoding="utf-8"))[0]
        self.assertEqual(record["story_format"], "HOSTING_OR_ARRIVAL")
        self.assertEqual(record["character_ar"], "عطار اسمه يوسف")

    def test_rotation_never_three_in_a_row(self) -> None:
        history = [{"story_format": "JOURNEY_QUEST"}, {"story_format": "JOURNEY_QUEST"}]
        self.assertFalse(generate_daily.story_format_allowed("JOURNEY_QUEST", history))
        self.assertTrue(generate_daily.story_format_allowed("MARKET_QUEST", history))
        self.assertTrue(generate_daily.story_format_allowed("JOURNEY_QUEST", [{"story_format": "CRAFT_QUEST"}, {"story_format": "JOURNEY_QUEST"}]))
        formats = ["JOURNEY_QUEST", "JOURNEY_QUEST", "MARKET_QUEST", "CRAFT_QUEST", "SEASONAL_TASK", "WORKSHOP_QUEST"]
        topic = run_choose(six(formats, [5, 5, 3, 3, 3, 3]), history)
        self.assertNotEqual(topic["story_format"], "JOURNEY_QUEST")

    def test_variety_breaks_near_ties_but_quality_wins_when_clearly_better(self) -> None:
        history = [{"story_format": "JOURNEY_QUEST"}, {"story_format": "MARKET_QUEST"}]
        near = [dict(candidate(1, "a", 5, "JOURNEY_QUEST")), dict(candidate(2, "b", 5, "CRAFT_QUEST"))]
        near[1]["scores"]["variety"] = 4
        self.assertEqual(generate_daily.pick_topic_by_format(near, history)["story_format"], "CRAFT_QUEST")
        clear = [candidate(1, "a", 5, "JOURNEY_QUEST"), candidate(2, "b", 3, "CRAFT_QUEST")]
        self.assertEqual(generate_daily.pick_topic_by_format(clear, history)["story_format"], "JOURNEY_QUEST")

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
        topic = {"topic_ar": "ت", "angle_ar": "ز", "story_format": "CRAFT_QUEST"}
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
        self.assertIn("الشخصية الخيالية المركّبة مسموحة لكن لا تُقدَّم كشخصية تاريخية حقيقية", prompts[0])
        self.assertIn("حالة المهمة", prompts[0])
        self.assertNotIn("لا تخترع شخصية", prompts[0])


class QualityGateTests(unittest.TestCase):
    NEW = generate_daily.STORY_QUALITY_ENGAGEMENT_GATES

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
        self.assertIn("المستمع يريد أن يبقى داخل العالم", prompt)
        self.assertIn("# 65. IMMERSIVE LOW-STAKES HISTORICAL QUEST", prompt)

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


class QualityRepairTests(unittest.TestCase):
    def passing_gate(self) -> dict:
        names = (
            "language", "adult_audience", "religion", "sleep", "historical_style",
            "no_dangerous_drift", "soft_narrative_spine", "gentle_curiosity",
            "attentional_anchoring", "sensory_imagery", "low_stakes", "gradual_unwind",
            "reentry_tolerance", "not_boring_or_overstimulating", "flexible_composition",
            *generate_daily.STORY_QUALITY_ENGAGEMENT_GATES,
        )
        return {"status": "PASS", **{name: True for name in names}, "issues": []}

    def test_curiosity_decay_failure_repairs_only_the_ending_then_passes(self) -> None:
        prefix = "جلس إبراهيم في الخان بهدوء ورتب الأواني قرب الباب. " * 900
        busy_tail = "ماذا سيحدث بعد قليل؟ ومن سيصل؟ وأين يذهب المسافر؟ " * 120
        story_text = prefix + "\n\n" + busy_tail
        self.assertTrue(any("curiosity does not decay" in e for e in generate_daily.quest_rules.story_text_errors(story_text)))

        gate_json = json.dumps(self.passing_gate(), ensure_ascii=False)

        def fake_gemini(prompt, *args, **kwargs):
            if "حوّل فقط الجمل الاستفهامية المتأخرة" in prompt:
                items = json.loads(prompt.split("الجمل:\n", 1)[1])
                return json.dumps({
                    "replacements": [
                        {
                            "index": item["index"],
                            "text": " ".join(
                                ["استمر"] + ["الهدوء"] * max(0, generate_daily.word_count(item["text"]) - 1)
                            ) + ".",
                        }
                        for item in items
                    ]
                }, ensure_ascii=False), []
            if kwargs.get("json_mode"):
                return gate_json, []
            raise AssertionError("late curiosity must use question-only repair")

        with tempfile.TemporaryDirectory() as directory:
            story = Path(directory)
            (story / "logs").mkdir()
            with mock.patch.object(generate_daily, "gemini", side_effect=fake_gemini) as gem:
                repaired, quality = generate_daily.ensure_story_quality(
                    story,
                    {"topic_ar": "خان حلب", "angle_ar": "استقبال القوافل", "quest_ar": "تجهيز الخان"},
                    "بحث موثق",
                    story_text,
                    label="test",
                )
            self.assertEqual(quality["status"], "PASS")
            self.assertTrue(repaired.startswith(prefix[:200]))
            self.assertNotIn("؟", " ".join(repaired.split()[int(len(repaired.split()) * 0.8):]))
            self.assertEqual(generate_daily.quest_rules.story_text_errors(repaired), [])
            self.assertEqual(gem.call_count, 2)  # one local repair + one final gate
            self.assertTrue(list((story / "logs").glob("quality_repair_01_ending.*")))

    def test_quality_stage_has_one_repair_attempt_only(self) -> None:
        self.assertEqual(generate_daily.STORY_QUALITY_REPAIR_ATTEMPTS, 1)

    def test_finalize_runs_one_final_quality_stage_after_fact_check(self) -> None:
        draft = "كلمة " * 5200
        passed = {"status": "PASS"}
        with tempfile.TemporaryDirectory() as directory:
            story = Path(directory)
            (story / "logs").mkdir()
            with mock.patch.object(generate_daily, "fact_check_story", return_value=(draft, passed)) as fact, \
                    mock.patch.object(generate_daily, "ensure_story_quality", return_value=(draft, passed)) as quality:
                result, quality_result, fact_result = generate_daily.finalize_story_text(
                    story, {"topic_ar": "ت", "angle_ar": "ز"}, "بحث", draft
                )
        self.assertEqual(result, draft)
        self.assertEqual((quality_result, fact_result), (passed, passed))
        fact.assert_called_once()
        quality.assert_called_once()
        self.assertEqual(quality.call_args.kwargs["label"], "final")

    def test_quality_repair_scope_is_targeted(self) -> None:
        self.assertEqual(
            generate_daily._quality_repair_scope({"deterministic": ["curiosity does not decay: x"]}),
            "ending",
        )
        self.assertEqual(
            generate_daily._quality_repair_scope({"deterministic": ["encyclopedic opening: x"]}),
            "opening",
        )
        self.assertIsNone(generate_daily._quality_repair_scope({"status": "REJECT"}))


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
        for token in ("ENGAGEMENT", "CALM ENGAGEMENT", "15–16", "الكاميرا السردية", "CALM_HOOK", "SOFT_FADE", "5000", "story_format", "CRAFT_QUEST"):
            self.assertIn(token, section)

    def test_fictional_composite_protagonist_is_allowed_but_never_presented_as_documented(self) -> None:
        rules = generate_daily.production_rules(22, 23, 64, 65)
        self.assertNotRegex(rules, r"ممنوع اختراع شخصية رئيسية خيالية")
        self.assertIn("COMPOSITE_FICTIONAL", rules)
        self.assertRegex(rules, r"يُمنع تقديمها كشخصية تاريخية حقيقية")
        self.assertIn("PLAUSIBLE FICTION, VERIFIED WORLD", rules)
        source = (ROOT / "scripts" / "generate_daily.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"لا\s+(?:تخترع|بطل خيالي|شخصية رئيسية خيالية)\s+شخصية")

    def test_prompts_include_new_rules(self) -> None:
        source = (ROOT / "scripts" / "generate_daily.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(re.findall(r"62, 64, 65\)", source)), 4)


if __name__ == "__main__":
    unittest.main()
