"""The Arabic thumbnail label is drawn by the deterministic renderer, never by the image model."""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily
import image_generate
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
LABEL = "أحواض نقع التوت"
TOPIC = {
    "topic_ar": "صناعة الورق في سمرقند", "angle_ar": "ليلة في ورشة ورق",
    "location_ar": "سمرقند", "period_ar": "العصر العباسي",
}
BACKGROUND_ONLY = (
    "A calm night scene of a Samarkand paper workshop with mulberry soaking basins and warm lanterns. "
    "Reserve a clean dark empty area on the left, about 30% of the frame. "
    "No text, no letters, no typography, no logos, no watermarks. "
    "16:9 YouTube thumbnail. Original composition. Professional quality."
)
SAMARKAND_BAD = (
    "A night scene of mulberry soaking basins in a Samarkand paper workshop, background scene with warm lanterns. "
    f"Large simple elegant typography in soft ivory reading {LABEL} in a clean dark area. "
    "No text covering most of the image. Do not render letters, words, typography, logos, or watermarks. "
    "16:9 YouTube thumbnail. Original composition. Professional quality."
)


def package(prompt: str, label: str = LABEL) -> dict:
    return {
        "episode_title_core": "ليلة هادئة في ورشة الورق بسمرقند",
        "title_suffix": "رحلة تاريخية هادئة للنوم 😴",
        "final_youtube_title": "ليلة هادئة في ورشة الورق بسمرقند | رحلة تاريخية هادئة للنوم 😴",
        "thumbnail_text": label,
        "thumbnail_prompt": prompt,
    }


def issues(prompt: str, label: str = LABEL) -> dict:
    return generate_daily.packaging_field_issues(package(prompt, label), TOPIC, [])


class PromptValidationTests(unittest.TestCase):
    def test_exact_thumbnail_text_in_prompt_fails(self):
        prompt = BACKGROUND_ONLY.replace("Reserve a clean", f"Near the basins {LABEL}. Reserve a clean")
        self.assertIn("exact thumbnail_text", issues(prompt)["thumbnail_prompt"])

    def test_typography_reading_arabic_text_fails(self):
        prompt = BACKGROUND_ONLY + f" Typography reading {LABEL}."
        self.assertIn("thumbnail_prompt", issues(prompt))

    def test_samarkand_prompt_with_both_instructions_fails_as_contradiction(self):
        result = issues(SAMARKAND_BAD)
        self.assertIn("thumbnail_prompt", result)
        self.assertIn("background-only", result["thumbnail_prompt"])
        # The same prompt also contains "Do not render letters ... typography": that must not rescue it.
        self.assertIn("do not render letters", SAMARKAND_BAD.lower())

    def test_positive_render_instruction_fails_even_with_no_text_elsewhere(self):
        for phrase in (
            "Write the words of the title across the dark area.",
            "Include the Arabic title in large elegant typography.",
            "Add text saying welcome.",
            "Render a caption in the dark area.",
            "Large simple elegant typography in soft ivory.",
        ):
            result = issues(BACKGROUND_ONLY + " " + phrase)
            self.assertIn("thumbnail_prompt", result, phrase)

    def test_any_arabic_in_prompt_fails(self):
        self.assertIn("Arabic", issues(BACKGROUND_ONLY + " ورق")["thumbnail_prompt"])

    def test_background_only_prompt_passes(self):
        self.assertEqual({}, issues(BACKGROUND_ONLY))

    def test_negative_forms_are_allowed(self):
        prompt = BACKGROUND_ONLY + " Do not render letters, words, typography, logos, or watermarks. Without any text overlay."
        self.assertEqual({}, issues(prompt))

    def test_rules_file_no_longer_asks_the_model_for_typography(self):
        rules = (ROOT / "config" / "youtube_packaging_rules.md").read_text(encoding="utf-8")
        self.assertNotIn("Include the exact Arabic thumbnail text", rules)
        self.assertIn("do NOT include the Arabic `thumbnail_text`", rules)
        self.assertIn("25-35%", rules)
        self.assertIn("deterministic renderer", rules)
        # Rest of the visual identity is kept.
        for kept in ("Low-key night lighting", "Palette: deep midnight navy", "16:9 YouTube thumbnail. Original composition."):
            self.assertIn(kept, rules)


class PackagingCacheAndImagePromptTests(unittest.TestCase):
    def test_stale_cache_with_old_rules_hash_is_regenerated_background_only(self):
        with tempfile.TemporaryDirectory() as directory:
            story = Path(directory) / "story"
            (story / "logs").mkdir(parents=True)
            old_package = package(SAMARKAND_BAD)
            topic_hash = hashlib.sha256(json.dumps(TOPIC, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            recent_hash = hashlib.sha256(json.dumps([], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            (story / "logs" / "youtube_packaging.json").write_text(json.dumps({
                "status": "PASS", "topic_sha256": topic_hash, "recent_packaging_sha256": recent_hash,
                "rules_sha256": "hash-of-the-old-rules", "package": old_package,
            }), encoding="utf-8")

            def fake_gemini(prompt, **_kw):
                if "أنت بوابة مستقلة لحزمة YouTube" in prompt:
                    return json.dumps({"status": "PASS", "title": True, "thumbnail_text": True,
                                       "thumbnail_prompt": True, "failing_fields": [], "issues": []}), []
                return json.dumps({
                    "episode_title_core": "ليلة هادئة في ورشة الورق بسمرقند",
                    "thumbnail_text": LABEL, "thumbnail_prompt": BACKGROUND_ONLY,
                }, ensure_ascii=False), []

            with mock.patch.object(generate_daily, "gemini", side_effect=fake_gemini) as gem, \
                 mock.patch.object(generate_daily, "recent_published_packaging", return_value=[]):
                result = generate_daily.generate_youtube_packaging(story, TOPIC)
        self.assertGreaterEqual(gem.call_count, 2)  # cache was NOT reused
        self.assertEqual(result["thumbnail_prompt"], BACKGROUND_ONLY)
        self.assertEqual(result["thumbnail_text"], LABEL)

    def test_story_and_audio_artifacts_are_not_inputs_of_packaging(self):
        source = inspect.getsource(generate_daily.generate_youtube_packaging)
        self.assertNotIn("12_audio", source)
        self.assertNotIn("draft_story", source)

    def test_image_prompt_file_never_contains_thumbnail_text(self):
        source = inspect.getsource(generate_daily.build_metadata_files)
        self.assertIn("thumbnail_prompt_text_conflicts(thumbnail_prompt", source)
        self.assertIn("PIPELINE SCENE RENDER: Generate the historical scene and reserved dark text area only", source)
        # A prompt that still carries the label is refused before any 08_image_prompt file is written.
        self.assertTrue(generate_daily.thumbnail_prompt_text_conflicts(SAMARKAND_BAD, LABEL))
        self.assertFalse(generate_daily.thumbnail_prompt_text_conflicts(BACKGROUND_ONLY, LABEL))

    def test_thumbnail_text_stays_in_metadata_field_and_renderer_uses_it(self):
        source = inspect.getsource(generate_daily.build_metadata_files)
        self.assertIn('"thumbnail_text": packaging["thumbnail_text"]', source)
        base = Image.new("RGB", (1280, 720), (10, 20, 40))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "t.jpg"
            try:
                image_generate.render_thumbnail(base, out, LABEL)
            except RuntimeError as exc:  # no Arabic font on this machine
                self.skipTest(str(exc))
            self.assertTrue(out.exists())
        with self.assertRaises(RuntimeError):
            image_generate.render_thumbnail(base, Path("x.jpg"), "كلمة")  # renderer enforces 2-4 words of the label

    def test_historical_accuracy_gate_still_rejects_generated_text(self):
        source = inspect.getsource(image_generate.historical_accuracy_gate)
        self.assertIn("IMAGE_ACCURACY_GATE_FAILED", source)
        self.assertIn("no_text_or_watermark", source)

    def test_image_retry_routing_unchanged(self):
        source = inspect.getsource(image_generate.generate_with_accuracy_retries)
        self.assertNotIn('SETTINGS["image_provider"] = "pollinations"', source)
        settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["image_accuracy_retry_attempts"], 4)


if __name__ == "__main__":
    unittest.main()
