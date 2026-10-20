from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline


ROOT = Path(__file__).resolve().parents[2]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
MASTER_RULES = (ROOT / "config" / "master_production_rules.md").read_text(encoding="utf-8")
TTS_RULES = (ROOT / "config" / "tts_speech_normalization_rules.md").read_text(encoding="utf-8")
OWNER_INVARIANTS = (ROOT / "config" / "owner_behavior_invariants.md").read_text(encoding="utf-8")
REVIEW_WORKFLOW = (ROOT / ".github" / "workflows" / "review.yml").read_text(encoding="utf-8")


class OwnerBehaviorInvariantTests(unittest.TestCase):
    def test_final_tts_is_unvocalized_by_contract_and_runtime(self) -> None:
        self.assertEqual(SETTINGS["arabic_diacritization"], "full_review_only")
        self.assertEqual(SETTINGS["tts_final_diacritics"], "none")
        sample = pipeline.api_text("هٰذِهِ جُمْلَةٌ هادِئَةٌ.")
        self.assertEqual(pipeline.DIACRITICS_RE.findall(sample), [])
        pipeline.require_no_tts_diacritics(sample)
        self.assertIn("خالية تمامًا", TTS_RULES)
        self.assertIn("07_story_tts_api.md", TTS_RULES)

    def test_write_for_ear_expands_units_and_abbreviations(self) -> None:
        self.assertIn("سم", TTS_RULES)
        self.assertIn("سنتيمتر", TTS_RULES)
        self.assertIn("اكتب للأذن لا للعين", TTS_RULES)
        violations = " ".join(pipeline.speech_safe_violations("بلغ عرضه خمسة سم."))
        self.assertIn("ambiguous abbreviation", violations)

    def test_story_opening_prohibitions_are_explicit(self) -> None:
        for phrase in ("مرحبًا بكم", "في هذا الوثائقي", "تعليمات التنفس", "شكرًا على الاستماع"):
            self.assertIn(phrase, MASTER_RULES)
        self.assertIn("تبدأ القصة بالمشهد نفسه", MASTER_RULES)

    def test_intentional_current_story_and_audio_choices_stay_pinned(self) -> None:
        self.assertEqual(float(SETTINGS["background_audio_volume"]), 0.03)
        self.assertIn("مهمة لطيفة منخفضة المخاطر", MASTER_RULES)
        self.assertIn("5000 كلمة عربية", MASTER_RULES)

    def test_review_approval_contract_is_preserved(self) -> None:
        self.assertEqual(SETTINGS["publish_requires"], "APPROVE_AND_PUBLISH")
        for action in ("publish", "reject", "regenerate", "notify-existing-review"):
            self.assertIn(action, REVIEW_WORKFLOW)
        self.assertIn("الموافقة الصريحة قبل النشر", OWNER_INVARIANTS)


if __name__ == "__main__":
    unittest.main()
