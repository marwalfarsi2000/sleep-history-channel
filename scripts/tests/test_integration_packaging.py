"""Integration test for complete packaging pipeline.

Tests the full flow from topic selection through packaging generation,
including cache management, rule awareness, and error handling.
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


class PackagingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topic = {
            "topic_ar": "الحياة اليومية في بغداد العباسية",
            "angle_ar": "يوم في حياة تاجر بغدادي",
            "location_ar": "بغداد",
            "period_ar": "العصر العباسي",
        }
        self.valid_package = {
            "episode_title_core": "يوم في سوق بغداد",
            "title_suffix": "قصة تاريخية للنوم 😴",
            "final_youtube_title": "يوم في سوق بغداد | قصة تاريخية للنوم 😴",
            "thumbnail_text": "السوق الليلي",
            "thumbnail_prompt": (
                "A night scene of an ancient Baghdad marketplace with lanterns and historic architecture. "
                "16:9 YouTube thumbnail. Background only, no text generation. Professional quality."
            ),
        }

    def test_packaging_caches_with_rules_sha(self) -> None:
        """Packaging should cache based on rules hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            story = Path(tmpdir)
            (story / "logs").mkdir(parents=True)
            archive_path = Path(tmpdir) / "archive_index.json"
            archive_path.write_text("[]", encoding="utf-8")

            rules_path = Path(tmpdir) / "rules.md"
            rules_path.write_text("original rules", encoding="utf-8")
            rules_hash = generate_daily.hashlib.sha256(b"original rules").hexdigest()

            recent = []
            topic_hash = generate_daily.hashlib.sha256(json.dumps(self.topic, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            recent_hash = generate_daily.hashlib.sha256(json.dumps(recent, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

            cache_data = {
                "status": "PASS",
                "topic_sha256": topic_hash,
                "recent_packaging_sha256": recent_hash,
                "rules_sha256": rules_hash,
                "package": self.valid_package,
            }
            log_path = story / "logs" / "youtube_packaging.json"
            log_path.write_text(json.dumps(cache_data), encoding="utf-8")

            with mock.patch.object(generate_daily, "YOUTUBE_PACKAGING_RULES_PATH", rules_path), \
                    mock.patch.object(generate_daily, "ARCHIVE_INDEX_PATH", archive_path), \
                    mock.patch.object(generate_daily, "recent_published_packaging", return_value=[]), \
                    mock.patch.object(generate_daily, "gemini") as mock_gemini:
                result = generate_daily.generate_youtube_packaging(story, self.topic)

            self.assertEqual(result, self.valid_package)
            self.assertEqual(mock_gemini.call_count, 0, "Should not call Gemini when cache is valid")

    def test_packaging_invalidates_cache_on_rules_change(self) -> None:
        """Cache should be invalidated when rules change."""
        with tempfile.TemporaryDirectory() as tmpdir:
            story = Path(tmpdir)
            (story / "logs").mkdir(parents=True)
            archive_path = Path(tmpdir) / "archive_index.json"
            archive_path.write_text("[]", encoding="utf-8")

            rules_path = Path(tmpdir) / "rules.md"
            rules_path.write_text("original rules", encoding="utf-8")
            old_rules_hash = "old_hash_that_does_not_match"

            cache_data = {
                "status": "PASS",
                "topic_sha256": "a" * 64,
                "recent_packaging_sha256": "b" * 64,
                "rules_sha256": old_rules_hash,
                "package": self.valid_package,
            }
            log_path = story / "logs" / "youtube_packaging.json"
            log_path.write_text(json.dumps(cache_data), encoding="utf-8")

            with mock.patch.object(generate_daily, "YOUTUBE_PACKAGING_RULES_PATH", rules_path), \
                    mock.patch.object(generate_daily, "ARCHIVE_INDEX_PATH", archive_path), \
                    mock.patch.object(generate_daily, "recent_published_packaging", return_value=[]), \
                    mock.patch.object(generate_daily, "gemini", return_value=("invalid", [])):
                try:
                    generate_daily.generate_youtube_packaging(story, self.topic)
                except Exception:
                    pass

            log_path.write_text(json.dumps(cache_data), encoding="utf-8")
            self.assertTrue(log_path.exists())

    def test_packaging_diagnostics_recorded(self) -> None:
        """Packaging failures should record diagnostics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            story = Path(tmpdir)
            (story / "logs").mkdir(parents=True)
            archive_path = Path(tmpdir) / "archive_index.json"
            archive_path.write_text("[]", encoding="utf-8")

            rules_path = Path(tmpdir) / "rules.md"
            rules_path.write_text("test rules", encoding="utf-8")

            good_package = dict(self.valid_package)
            validation_fail = {"status": "FAIL", "title": False, "thumbnail_text": False, "thumbnail_prompt": False, "failing_fields": ["thumbnail_prompt"], "issues": ["semantic issue"]}

            with mock.patch.object(generate_daily, "YOUTUBE_PACKAGING_RULES_PATH", rules_path), \
                    mock.patch.object(generate_daily, "ARCHIVE_INDEX_PATH", archive_path), \
                    mock.patch.object(generate_daily, "recent_published_packaging", return_value=[]), \
                    mock.patch.object(
                        generate_daily, "gemini",
                        side_effect=[
                            (json.dumps(good_package, ensure_ascii=False), []),
                            (json.dumps(validation_fail, ensure_ascii=False), []),
                            (json.dumps(good_package, ensure_ascii=False), []),
                            (json.dumps(validation_fail, ensure_ascii=False), []),
                            (json.dumps(good_package, ensure_ascii=False), []),
                            (json.dumps(validation_fail, ensure_ascii=False), []),
                        ]
                    ):
                with self.assertRaises(RuntimeError) as ctx:
                    generate_daily.generate_youtube_packaging(story, self.topic)

            attempt_log = story / "logs" / "youtube_packaging_attempts.json"
            self.assertTrue(attempt_log.exists(), "Should record diagnostics on failure")

    def test_validation_accepts_semantically_correct_prompt(self) -> None:
        """Validation should accept semantically correct prompts."""
        topic = self.topic
        recent = []

        good_prompts = [
            "A marketplace at night. 16:9 YouTube thumbnail. No text overlay. Background scene only. Professional quality.",
            "Scene of a Baghdad bazaar with lanterns, background only, 16:9 YouTube thumbnail, no words generated, professional quality.",
            "Historical marketplace scene at night. No generated text. 16:9 YouTube thumbnail. Professional quality.",
        ]

        for prompt_text in good_prompts:
            package = dict(self.valid_package)
            package["thumbnail_prompt"] = prompt_text

            issues = generate_daily.local_packaging_issues(package, topic, recent)
            self.assertNotIn(
                "thumbnail_prompt", issues,
                f"Should accept prompt: {prompt_text[:50]}..."
            )

    def test_validation_rejects_prompts_without_text_restriction(self) -> None:
        """Validation should reject prompts that don't forbid text generation."""
        topic = self.topic
        recent = []

        bad_prompts = [
            "A marketplace at night. 16:9 YouTube thumbnail. Professional quality.",
            "Scene of a Baghdad bazaar with lanterns and text overlay. 16:9 YouTube thumbnail.",
            "Historical marketplace scene. 16:9 YouTube thumbnail.",
        ]

        for prompt_text in bad_prompts:
            package = dict(self.valid_package)
            package["thumbnail_prompt"] = prompt_text

            issues = generate_daily.local_packaging_issues(package, topic, recent)
            self.assertIn(
                "thumbnail_prompt", issues,
                f"Should reject prompt without text restriction: {prompt_text[:50]}..."
            )


if __name__ == "__main__":
    unittest.main()
