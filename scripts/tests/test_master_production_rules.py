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
