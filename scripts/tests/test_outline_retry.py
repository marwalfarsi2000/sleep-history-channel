from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily

TOPIC = {"topic_ar": "ت", "angle_ar": "ز", "story_format": "JOURNEY"}


def scene(n: int = 1) -> dict:
    return {
        "place_or_anchor": f"مكان {n}", "action": "نقترب", "main_idea": f"فكرة {n}",
        "gentle_question": "من يعبر؟", "transition": "ثم ندخل",
    }


def outline(count: int = 8) -> list[dict]:
    phases = list(generate_daily.STORY_PHASES)
    chosen = [phases[min(i * len(phases) // count, len(phases) - 1)] for i in range(count)]
    chosen[0], chosen[-1] = phases[0], phases[-1]
    return [
        {"title": f"قسم {i}", "phase": phase, "brief": "x", "scenes": [scene(1), scene(2)], "target_words": 750}
        for i, phase in enumerate(chosen)
    ]


def reply(sections) -> tuple[str, list]:
    return json.dumps({"sections": sections}, ensure_ascii=False), []


MALFORMED = ('{"sections":[{"title":"x" "phase"', [])


def run(responses, topic=TOPIC):
    with mock.patch.object(generate_daily, "gemini", side_effect=responses) as gem:
        try:
            return generate_daily.make_outline(topic, "بحث"), gem
        except RuntimeError as exc:
            exc.gem = gem
            raise


class OutlineRetryTests(unittest.TestCase):
    def test_retry_limit_constant(self) -> None:
        self.assertEqual(generate_daily.OUTLINE_RETRY_MAX, 3)

    def test_1_valid_first_attempt_single_call(self) -> None:
        good = outline()
        sections, gem = run([reply(good)])
        self.assertEqual(gem.call_count, 1)
        self.assertEqual(sections, good)

    def test_2_malformed_json_then_valid(self) -> None:
        sections, gem = run([MALFORMED, reply(outline())])
        self.assertEqual(gem.call_count, 2)
        self.assertEqual(len(sections), 8)
        second = gem.call_args_list[1].args[0]
        self.assertIn("Previous outline response was invalid JSON", second)
        self.assertIn("Return one complete valid JSON object only", second)
        self.assertNotIn('"sections":[{"title":"x" "phase"', second)  # raw response not resent

    def test_3_too_few_sections_then_valid(self) -> None:
        sections, gem = run([reply(outline(6)), reply(outline(9))])
        self.assertEqual(gem.call_count, 2)
        self.assertEqual(len(sections), 9)
        self.assertIn("Outline must contain 7-10 macro sections", gem.call_args_list[1].args[0])
        self.assertIn("Previous outline failed validation", gem.call_args_list[1].args[0])

    def test_4_bad_phase_order_then_valid(self) -> None:
        bad = outline()
        bad[2]["phase"], bad[5]["phase"] = bad[5]["phase"], bad[2]["phase"]
        _, gem = run([reply(bad), reply(outline())])
        self.assertEqual(gem.call_count, 2)

    def test_5_missing_scene_field_then_valid(self) -> None:
        bad = outline()
        del bad[3]["scenes"][0]["main_idea"]
        _, gem = run([reply(bad), reply(outline())])
        self.assertEqual(gem.call_count, 2)
        self.assertIn("main_idea", gem.call_args_list[1].args[0])

    def test_scene_count_bounds_still_enforced(self) -> None:
        for scenes in ([scene()], [scene(i) for i in range(5)]):
            bad = outline()
            bad[1]["scenes"] = scenes
            _, gem = run([reply(bad), reply(outline())])
            self.assertEqual(gem.call_count, 2)

    def test_non_object_responses_are_validation_failures(self) -> None:
        _, gem = run([("[1, 2]", []), reply([1, 2, 3]), reply(outline())])
        self.assertEqual(gem.call_count, 3)

    def test_6_three_malformed_fail_with_last_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, r"OUTLINE_GENERATION_FAILED_AFTER_RETRIES: JSONDecodeError") as ctx:
            run([MALFORMED] * 3)
        self.assertEqual(ctx.exception.gem.call_count, 3)

    def test_7_three_validation_failures_fail_with_last_error(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError, r"OUTLINE_GENERATION_FAILED_AFTER_RETRIES: Outline must contain 7-10 macro sections"
        ) as ctx:
            run([reply(outline(6))] * 3)
        self.assertEqual(ctx.exception.gem.call_count, 3)

    def test_8_returns_last_successful_not_an_earlier_attempt(self) -> None:
        first, second = outline(6), outline(10)
        sections, _ = run([reply(first), reply(second)])
        self.assertEqual(sections, second)
        self.assertEqual(len(sections), 10)

    def test_9_signature_unchanged(self) -> None:
        self.assertEqual(
            list(inspect.signature(generate_daily.make_outline).parameters), ["topic", "research"]
        )

    def test_validator_contract_not_loosened(self) -> None:
        for count in (6, 11):
            with self.assertRaises(RuntimeError):
                generate_daily.validate_outline_sections(outline(count))
        generate_daily.validate_outline_sections(outline(7))
        generate_daily.validate_outline_sections(outline(10))
        self.assertEqual(
            generate_daily.STORY_PHASES, ("CALM_HOOK", "EXPLORATION", "SETTLE", "UNWIND", "SOFT_FADE")
        )

    def test_unrelated_errors_are_not_swallowed(self) -> None:
        for error in (RuntimeError("WAITING_FOR_LLM_QUOTA"), PermissionError("disk"), KeyError("x")):
            with self.assertRaises(type(error)) as ctx:
                run([error])
            self.assertEqual(ctx.exception.gem.call_count if hasattr(ctx.exception, "gem") else 1, 1)
        with mock.patch.object(generate_daily, "gemini", side_effect=RuntimeError("auth failed")) as gem:
            with self.assertRaisesRegex(RuntimeError, "auth failed"):
                generate_daily.make_outline(TOPIC, "بحث")
        self.assertEqual(gem.call_count, 1)

    def test_attempt_logging_is_safe_and_on_stderr(self) -> None:
        with mock.patch("sys.stderr") as err:
            run([MALFORMED, reply(outline(6)), reply(outline())])
        written = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("OUTLINE_ATTEMPT=1 PARSE=FAIL VALIDATION=NOT_RUN", written)
        self.assertIn("OUTLINE_ATTEMPT=2 PARSE=PASS VALIDATION=FAIL", written)
        self.assertIn("OUTLINE_ATTEMPT=3 PARSE=PASS VALIDATION=PASS", written)
        self.assertNotIn("بحث", written)


class ResumeStillReusesWorkTests(unittest.TestCase):
    def _story(self, root: Path) -> Path:
        story = root / "stories" / "2026-10-18_samarkand-paper-making-daily-life"
        (story / "logs").mkdir(parents=True)
        topic = {**TOPIC, "slug": "samarkand-paper-making-daily-life"}
        (story / "logs/topic.json").write_text(json.dumps(topic), encoding="utf-8")
        (story / "logs/research_notes_draft.md").write_text("بحث محفوظ", encoding="utf-8")
        return story

    def test_10_11_resume_reuses_topic_and_research_and_caller_saves_outline(self) -> None:
        class Stop(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = self._story(root)
            sections = outline()
            with mock.patch.object(generate_daily, "ROOT", root), \
                    mock.patch.object(generate_daily, "research_topic") as research, \
                    mock.patch.object(generate_daily, "choose_topic") as choose, \
                    mock.patch.object(generate_daily, "make_outline", return_value=sections) as make, \
                    mock.patch.object(generate_daily, "draft_story", side_effect=Stop):
                with self.assertRaises(Stop):
                    generate_daily.resume_partial_story(story)
            saved = json.loads((story / "logs/outline.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, {"sections": sections})
            research.assert_not_called()
            choose.assert_not_called()
            make.assert_called_once()
            self.assertEqual(make.call_args.args[1], "بحث محفوظ")
            self.assertTrue((story / "logs/topic.json").exists())
            self.assertTrue((story / "logs/research_notes_draft.md").exists())

    def test_existing_outline_is_reused_without_a_model_call(self) -> None:
        class Stop(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = self._story(root)
            (story / "logs/outline.json").write_text(json.dumps({"sections": outline()}), encoding="utf-8")
            with mock.patch.object(generate_daily, "ROOT", root), \
                    mock.patch.object(generate_daily, "make_outline") as make, \
                    mock.patch.object(generate_daily, "draft_story", side_effect=Stop):
                with self.assertRaises(Stop):
                    generate_daily.resume_partial_story(story)
            make.assert_not_called()

    def test_failed_outline_leaves_checkpoint_files_intact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = self._story(root)
            with mock.patch.object(generate_daily, "ROOT", root), \
                    mock.patch.object(generate_daily, "gemini", return_value=MALFORMED):
                with self.assertRaisesRegex(RuntimeError, "OUTLINE_GENERATION_FAILED_AFTER_RETRIES"):
                    generate_daily.resume_partial_story(story)
            self.assertFalse((story / "logs/outline.json").exists())
            self.assertTrue((story / "logs/topic.json").exists())
            self.assertTrue((story / "logs/research_notes_draft.md").exists())


if __name__ == "__main__":
    unittest.main()
