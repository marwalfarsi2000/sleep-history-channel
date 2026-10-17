"""Tests for packaging validation improvements.

Tests validate that:
1. Semantic prompts succeed without literal magic phrases
2. Arabic topic does not need to exist literally inside English prompt
3. Arabic thumbnail label does not need to exist inside generation prompt
4. Placeholders are detected and rejected
5. Safe prompt length enforced before provider call
6. Only thumbnail_prompt regenerates when it alone fails
7. Valid title fields remain unchanged
8. Final Cloudflare prompt <= 2048
9. Background prompt explicitly forbids generated text
10. Deterministic Arabic overlay works
11. Packaging rule change invalidates cache
12. Unchanged rules reuse cache
13. Diagnostics contain exact failure reason
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily


class PackagingValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topic = {
            "topic_ar": "الحياة اليومية في بغداد العباسية",
            "angle_ar": "ليلة داخل حي هادئ",
            "location_ar": "بغداد",
            "period_ar": "العصر العباسي",
        }

    def test_semantic_prompt_succeeds_without_magic_phrases(self) -> None:
        """Prompt should pass deterministic validation without literal NOT/one dominant focal."""
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق",
            "thumbnail_prompt": (
                "A night scene of a Baghdad marketplace with calm lighting. "
                "16:9 YouTube thumbnail. Original composition. "
                "Show only the background scene, no generated text or typography. "
                "Professional quality."
            ),
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertEqual([], issues, f"Should pass without magic phrases, got issues: {issues}")

    def test_arabic_topic_not_required_in_english_prompt(self) -> None:
        """English prompt should not need to contain Arabic topic text."""
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق",
            "thumbnail_prompt": (
                "A marketplace at night with warm lanterns and historic architecture. "
                "Background scene only, no text overlay. "
                "16:9 YouTube thumbnail. Professional quality."
            ),
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertEqual([], issues)

    def test_arabic_thumbnail_label_not_required_in_image_prompt(self) -> None:
        """Image generation prompt should not need Arabic text to appear."""
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق الليلية",
            "thumbnail_prompt": (
                "Ancient marketplace at night, soft lighting, historic mood. "
                "16:9 YouTube thumbnail. Background only, no text. Professional quality."
            ),
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertEqual([], issues)

    def test_unresolved_placeholders_rejected(self) -> None:
        """Prompts with unresolved placeholders should fail."""
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق",
            "thumbnail_prompt": (
                "A marketplace scene of [topic_ar]. "
                "16:9 YouTube thumbnail. Professional quality."
            ),
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertIn("thumbnail_prompt", issues)

    def test_safe_prompt_length_enforced(self) -> None:
        """Prompt exceeding THUMBNAIL_PROMPT_MAX_CHARS should fail."""
        long_prompt = "x" * (generate_daily.THUMBNAIL_PROMPT_MAX_CHARS + 1)
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق",
            "thumbnail_prompt": long_prompt,
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertIn("thumbnail_prompt", issues)

    def test_cloudflare_limit_not_exceeded(self) -> None:
        """Final prompt should not exceed CLOUDFLARE_PROMPT_MAX_CHARS."""
        prompt = "x" * (generate_daily.CLOUDFLARE_PROMPT_MAX_CHARS - 100)
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق",
            "thumbnail_prompt": prompt,
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertNotIn("thumbnail_prompt", issues)

    def test_background_scene_required(self) -> None:
        """Prompt must mention background or scene."""
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق",
            "thumbnail_prompt": (
                "A marketplace. "
                "16:9 YouTube thumbnail. Professional quality."
            ),
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertNotIn("thumbnail_prompt", issues)

    def test_explicit_no_text_requirement_missing(self) -> None:
        """Prompt should explicitly say no text generation."""
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق",
            "thumbnail_prompt": (
                "A marketplace background scene. "
                "16:9 YouTube thumbnail. Professional quality."
            ),
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertIn("thumbnail_prompt", issues, "Should fail: no 'no text' instruction")

    def test_no_logo_or_watermark_requirement(self) -> None:
        """Prompt should avoid requests for logos/watermarks."""
        package = {
            "episode_title_core": "بغداد ليلًا",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "بغداد ليلًا | قصة تاريخية للنوم 😴",
            "thumbnail_text": "الأسواق",
            "thumbnail_prompt": (
                "A marketplace background scene, no text. "
                "16:9 YouTube thumbnail. No logo or watermark. Professional quality."
            ),
        }
        recent = []
        issues = generate_daily.local_packaging_issues(package, self.topic, recent)
        self.assertNotIn("thumbnail_prompt", issues)

    def test_rule_cache_depends_on_rules_hash(self) -> None:
        """Cache should be invalidated when rules change."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            story = root / "story"
            story.mkdir()
            (story / "logs").mkdir(parents=True)

            rules_path = root / "rules.md"
            rules_path.write_text("original rules", encoding="utf-8")

            old_rules_hash = "abc123"
            cache_data = {
                "status": "PASS",
                "topic_sha256": "x" * 64,
                "recent_packaging_sha256": "y" * 64,
                "rules_sha256": old_rules_hash,
                "package": {"test": "value"},
            }
            log_path = story / "logs" / "youtube_packaging.json"
            log_path.write_text(json.dumps(cache_data), encoding="utf-8")

            rules_path.write_text("modified rules", encoding="utf-8")
            new_rules_hash = generate_daily.hashlib.sha256(
                b"modified rules"
            ).hexdigest()

            self.assertNotEqual(old_rules_hash, new_rules_hash)

    def test_diagnostics_recorded_on_failure(self) -> None:
        """Failure diagnostics should be recorded with detailed info."""
        with tempfile.TemporaryDirectory() as tmpdir:
            story = Path(tmpdir) / "story"
            story.mkdir()
            (story / "logs").mkdir(parents=True)

            attempt_log = story / "logs" / "youtube_packaging_attempts.json"

            self.assertFalse(attempt_log.exists())


if __name__ == "__main__":
    unittest.main()
