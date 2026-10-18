from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily
from test_sleep_first_selection import SCORE_NAMES, candidate
from test_engagement_first_storytelling import GATE


def fresh(count: int, families: int, score: int = 4) -> list[dict]:
    return [candidate(i + 1, f"fam{i % families}", score) for i in range(count)]


def banked(index: int, family: str, status: str = "queued", score: int = 4) -> dict:
    item = candidate(index, family, score)
    item.update(bank_status=status, sleep_score_average=float(score))
    return item


def run(candidates: list[dict], bank: list[dict] | None = None, history: list[dict] | None = None):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "bank.json"
        path.write_text(json.dumps(bank or []), encoding="utf-8")
        with mock.patch.object(generate_daily, "TOPIC_CANDIDATE_BANK_PATH", path), \
                mock.patch.object(generate_daily, "gemini", side_effect=[
                    (json.dumps({"candidates": candidates}, ensure_ascii=False), []),
                    (json.dumps(GATE), []),
                ]) as gem:
            topic = generate_daily.choose_topic(history or [])
        return topic, gem


class TopicDiscoveryMinimumTests(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(generate_daily.PREFERRED_CANDIDATES, 6)
        self.assertEqual(generate_daily.MIN_VALID_CANDIDATES, 4)
        self.assertEqual(generate_daily.MIN_NARRATIVE_FAMILIES, 4)

    def test_prompt_still_asks_for_six_to_eight(self) -> None:
        _, gem = run(fresh(6, 6))
        self.assertIn("من ستة إلى ثمانية مرشحين", gem.call_args_list[0].args[0])

    def test_six_candidates_pass(self) -> None:
        topic, _ = run(fresh(6, 6))
        self.assertEqual(topic["discovery_candidate_count"], 6)

    def test_five_candidates_four_families_pass(self) -> None:
        run(fresh(5, 4))

    def test_four_candidates_four_families_pass(self) -> None:
        run(fresh(4, 4))

    def test_three_candidates_fail(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "TOPIC_DISCOVERY_FAILED: fewer than 4 valid"):
            run(fresh(3, 3))

    def test_four_candidates_three_families_fail(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "TOPIC_DISCOVERY_FAILED: fewer than 4 narrative"):
            run(fresh(4, 3))

    def test_queued_bank_completes_the_pool(self) -> None:
        bank = [banked(10, "bank-a"), banked(11, "bank-b")]
        topic, _ = run(fresh(2, 2), bank)
        self.assertEqual(topic["slug"] in {f"topic-{i}" for i in (1, 2, 10, 11)}, True)

    def test_bank_alone_cannot_hide_a_family_shortage(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "narrative families"):
            run(fresh(2, 1), [banked(10, "fam0"), banked(11, "fam0")])

    def test_used_selected_and_rejected_bank_entries_are_not_used(self) -> None:
        bank = [
            banked(10, "a", "selected", 5), banked(11, "b", "rejected", 5),
            banked(12, "c", "queued", 5), banked(13, "d", "queued", 5),
        ]
        history = [{"slug": "topic-12", "story_format": "HOW_IT_WORKED"}]
        with self.assertRaisesRegex(RuntimeError, "fewer than 4 valid"):
            run(fresh(2, 2), bank, history)
        # Selected/rejected/used entries never win even with top scores.
        topic, _ = run(fresh(3, 3) + [candidate(14, "z", 3)], bank, history)
        self.assertNotIn(topic["slug"], {"topic-10", "topic-11", "topic-12"})

    def test_fresh_candidate_cannot_revive_rejected_or_used_slug(self) -> None:
        bank = [banked(10, "a", "rejected", 5)]
        again = candidate(10, "a", 5)
        with self.assertRaisesRegex(RuntimeError, "fewer than 4 valid"):
            run([again, *fresh(3, 3)[1:]], bank)

    def test_invalid_candidates_are_dropped_before_counting(self) -> None:
        items = fresh(5, 5)
        items[0]["scores"]["novelty_variety"] = 9
        run(items)
        items = fresh(4, 4)
        items[0]["scores"]["novelty_variety"] = 9
        with self.assertRaisesRegex(RuntimeError, "fewer than 4 valid"):
            run(items)


if __name__ == "__main__":
    unittest.main()
