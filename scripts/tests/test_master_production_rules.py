from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily


class MasterProductionRulesTests(unittest.TestCase):
    def test_extracts_only_requested_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rules = Path(temp_dir) / "rules.md"
            rules.write_text(
                "# 1. ONE\nfirst\n\n---\n\n# 2. TWO\nsecond\n\n---\n\n# 3. THREE\nthird\n",
                encoding="utf-8",
            )
            with mock.patch.object(generate_daily, "MASTER_RULES_PATH", rules):
                selected = generate_daily.production_rules(1, 3)

        self.assertIn("# 1. ONE", selected)
        self.assertIn("# 3. THREE", selected)
        self.assertNotIn("# 2. TWO", selected)

    def test_missing_section_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rules = Path(temp_dir) / "rules.md"
            rules.write_text("# 1. ONE\nfirst\n", encoding="utf-8")
            with mock.patch.object(generate_daily, "MASTER_RULES_PATH", rules):
                with self.assertRaisesRegex(RuntimeError, "Missing master"):
                    generate_daily.production_rules(2)


if __name__ == "__main__":
    unittest.main()


class RulesMatchRuntimeTests(unittest.TestCase):
    """The written rules and the runtime constants must never silently disagree."""

    def setUp(self) -> None:
        self.rules = generate_daily.MASTER_RULES_PATH.read_text(encoding="utf-8")

    def test_candidate_minimums_are_documented_as_an_explicit_fallback(self) -> None:
        self.assertEqual(
            (generate_daily.PREFERRED_CANDIDATES, generate_daily.MIN_VALID_CANDIDATES, generate_daily.MIN_NARRATIVE_FAMILIES),
            (6, 4, 4),
        )
        self.assertIn("PREFERRED_CANDIDATES", self.rules)
        self.assertIn("MIN_VALID_CANDIDATES = 4", self.rules)
        self.assertIn("MIN_NARRATIVE_FAMILIES = 4", self.rules)
        self.assertNotIn("أنشئ ستة مرشحين متنوعين على الأقل", self.rules)

    def test_outline_limits_match_the_rules(self) -> None:
        self.assertEqual((generate_daily.MIN_MACRO_SECTIONS, generate_daily.MAX_MACRO_SECTIONS), (7, 10))
        self.assertIn("من 7 إلى 10 أقسام كبرى", self.rules)
        self.assertEqual(generate_daily.MIN_OUTLINE_TARGET_WORDS, 5500)
        self.assertIn("لا يقل عن 5500", self.rules)

    def test_research_alignment_gate_is_a_written_hard_rule(self) -> None:
        self.assertIn("RESEARCH_TOPIC_ALIGNMENT_GATE", self.rules)
        self.assertIn("REANCHOR_REQUIRED", self.rules)
        self.assertIn("RESEARCH_INSUFFICIENT", self.rules)
