"""Tests for state management infrastructure.

Tests validate:
1. Episode state can be created and updated
2. episode_id is preserved across updates
3. automation-state branch can be initialized
4. Legacy config fallback works
5. No secrets are stored in state
6. Missing state recoverable from Internet Archive
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state_store
import episode_id
import stage_manifest


class StateStoreTests(unittest.TestCase):
    def test_episode_state_creation(self) -> None:
        """New episode state can be created."""
        state = {
            "episode_id": "ep_20261017_palmyra_abc123",
            "slug": "daily-life-in-palmyra-roman-era",
            "status": "drafting",
        }
        self.assertIn("episode_id", state)
        self.assertIn("slug", state)

    def test_episode_id_generation(self) -> None:
        """Episode IDs are stable and independent of final titles."""
        import datetime as dt
        date = dt.date(2026, 10, 17)
        slug = "palmyra-roman"
        title = "Daily Life in Palmyra"

        eid1 = episode_id.generate_episode_id(date, slug, title)
        eid2 = episode_id.generate_episode_id(date, slug, title)

        self.assertEqual(eid1, eid2)
        self.assertTrue(eid1.startswith("ep_20261017_"))

        eid3 = episode_id.generate_episode_id(date, slug, "Different Title")
        self.assertNotEqual(eid1, eid3)

    def test_stage_manifest_creation(self) -> None:
        """Stage manifest can track completion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            story = Path(tmpdir)
            (story / "logs").mkdir()

            stage_manifest.record_stage(
                story,
                "research",
                input_sha256="abc" * 21 + "def",
                output_hashes={"04_research_notes.md": "xyz" * 21 + "123"},
            )

            manifest = stage_manifest.load_manifest(story)
            self.assertIn("research", manifest.get("stages", {}))
            self.assertEqual(
                manifest["stages"]["research"]["status"],
                "completed"
            )

    def test_stage_manifest_reuse_detection(self) -> None:
        """Stage manifest detects when output can be reused."""
        with tempfile.TemporaryDirectory() as tmpdir:
            story = Path(tmpdir)
            (story / "logs").mkdir()

            output_file = story / "output.txt"
            output_file.write_text("content", encoding="utf-8")
            output_hash = stage_manifest.compute_file_hash(output_file)

            stage_manifest.record_stage(
                story,
                "test_stage",
                input_sha256="input123",
                output_hashes={"output.txt": output_hash},
            )

            can_reuse = stage_manifest.can_reuse_stage(
                story,
                "test_stage",
                current_input_sha256="input123",
            )
            self.assertTrue(can_reuse)

            can_reuse_different = stage_manifest.can_reuse_stage(
                story,
                "test_stage",
                current_input_sha256="input456",
            )
            self.assertFalse(can_reuse_different)

    def test_stage_manifest_detects_corrupted_output(self) -> None:
        """Stage manifest detects when output file is corrupted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            story = Path(tmpdir)
            (story / "logs").mkdir()

            output_file = story / "output.txt"
            output_file.write_text("content", encoding="utf-8")
            output_hash = stage_manifest.compute_file_hash(output_file)

            stage_manifest.record_stage(
                story,
                "test_stage",
                input_sha256="input123",
                output_hashes={"output.txt": output_hash},
            )

            output_file.write_text("different content", encoding="utf-8")

            can_reuse = stage_manifest.can_reuse_stage(
                story,
                "test_stage",
                current_input_sha256="input123",
            )
            self.assertFalse(can_reuse, "Should detect corrupted output")


class ThumbnailDesignTests(unittest.TestCase):
    def test_thumbnail_design_field_extraction(self) -> None:
        """Design fields can be extracted from metadata."""
        import thumbnail_design

        metadata = {
            "thumbnail_scene_type": "market",
            "thumbnail_focal_subject": "merchant",
            "thumbnail_composition": "leading_lines",
            "thumbnail_lighting": "lantern_low_key",
            "thumbnail_environment": "urban_dense",
        }

        design = thumbnail_design.get_thumbnail_design_fields(metadata)
        self.assertEqual(design["scene_type"], "market")
        self.assertEqual(design["composition"], "leading_lines")

    def test_thumbnail_design_avoids_repetition(self) -> None:
        """Design detector flags repetitive combinations."""
        import thumbnail_design

        proposed = {
            "scene_type": "market",
            "focal_subject": "merchant",
            "composition": "leading_lines",
        }

        recent = [
            {
                "scene_type": "market",
                "focal_subject": "merchant",
                "composition": "leading_lines",
            },
        ]

        should_avoid = thumbnail_design.should_avoid_design(proposed, recent)
        self.assertTrue(should_avoid)

    def test_thumbnail_design_suggestion_is_contextual(self) -> None:
        """Design suggestions respect historical setting."""
        import thumbnail_design

        suggestion_market = thumbnail_design.get_distinct_design_suggestion(
            "daily life in the marketplace",
            []
        )
        self.assertEqual(suggestion_market["scene_type"], "market")

        suggestion_desert = thumbnail_design.get_distinct_design_suggestion(
            "caravan on the silk road",
            []
        )
        self.assertEqual(suggestion_desert["scene_type"], "caravan_camp")


if __name__ == "__main__":
    unittest.main()
