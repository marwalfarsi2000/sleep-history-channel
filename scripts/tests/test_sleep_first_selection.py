from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily


SCORE_NAMES = (
    "gentle_engagement", "low_cognitive_arousal", "narrative_flow",
    "sensory_potential", "reentry_tolerance", "low_stakes",
    "gradual_unwind_potential", "novelty_variety",
)


def candidate(index: int, family: str, score: int) -> dict:
    return {
        "topic_ar": f"موضوع {index}",
        "angle_ar": f"زاوية {index}",
        "slug": f"topic-{index}",
        "search_query_en": f"historical topic {index}",
        "period_ar": "فترة تاريخية",
        "location_ar": "مدينة تاريخية",
        "family": family,
        "key_elements": ["السوق", "الحرفة"],
        "reason_ar": "مناسب للنوم",
        "narrative_follow": "مسار يوم هادئ",
        "soft_narrative_spine": "من الفجر إلى المساء",
        "gentle_curiosity": "كيف يبدأ العمل اليومي؟",
        "attentional_anchor": "حرفي يسير في المدينة",
        "sensory_potential_notes": "الضوء والأصوات البعيدة",
        "tension_risks": [],
        "reentry_plan": "مشاهد مستقلة مترابطة",
        "long_form_capacity": "أماكن وحرف متنوعة بلا حشو",
        "difference_from_recent": "عائلة ومكان مختلفان",
        "scores": {name: score for name in SCORE_NAMES},
    }


class SleepFirstSelectionTests(unittest.TestCase):
    def test_discovers_diverse_candidates_then_selects_through_hard_gate(self) -> None:
        candidates = [
            candidate(1, "day-in-life", 3),
            candidate(2, "journey", 3),
            candidate(3, "place", 3),
            candidate(4, "craft", 3),
            candidate(5, "ordinary-life", 4),
            candidate(6, "seasonal-rhythm", 5),
        ]
        gate = {
            "status": "PASS",
            "language": True,
            "religion": True,
            "sleep": True,
            "quality": True,
            "universality": True,
            "non_duplicate": True,
            "soft_narrative_spine": True,
            "gentle_curiosity": True,
            "attentional_anchoring": True,
            "reentry_tolerance": True,
            "gradual_unwind": True,
            "long_form_without_filler": True,
            "scores": {name: 5 for name in SCORE_NAMES},
            "reasons": [],
        }
        with mock.patch.object(
            generate_daily,
            "gemini",
            side_effect=[
                (json.dumps({"candidates": candidates}, ensure_ascii=False), []),
                (json.dumps(gate, ensure_ascii=False), []),
            ],
        ) as gemini:
            selected = generate_daily.choose_topic([])

        self.assertEqual(selected["slug"], "topic-6")
        self.assertEqual(selected["discovery_candidate_count"], 6)
        self.assertGreaterEqual(selected["discovery_family_count"], 4)
        self.assertIn("# 62. SLEEP-FIRST", gemini.call_args_list[0].args[0])
        self.assertIn("# 62. SLEEP-FIRST", gemini.call_args_list[1].args[0])

    def test_discovery_rejects_insufficient_narrative_variety(self) -> None:
        candidates = [candidate(index, "same-family", 5) for index in range(1, 7)]
        with mock.patch.object(
            generate_daily,
            "gemini",
            return_value=(json.dumps({"candidates": candidates}, ensure_ascii=False), []),
        ):
            with self.assertRaisesRegex(RuntimeError, "fewer than 4 narrative families"):
                generate_daily.choose_topic([])


if __name__ == "__main__":
    unittest.main()
