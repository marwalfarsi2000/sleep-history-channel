import inspect
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_daily  # noqa: E402
import image_generate  # noqa: E402
import pipeline  # noqa: E402


class BackgroundVolumeTests(unittest.TestCase):
    def test_setting_is_three_percent(self):
        settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["background_audio_volume"], 0.03)
        self.assertEqual(pipeline.background_volume_percent(), 3)

    def test_metadata_percent_not_hardcoded(self):
        source = inspect.getsource(pipeline)
        self.assertNotIn("background_audio_volume_percent=15", source)
        self.assertNotIn("بنسبة 15%", source)
        with mock.patch.dict(pipeline.SETTINGS, {"background_audio_volume": 0.07}):
            self.assertEqual(pipeline.background_volume_percent(), 7)

    def test_mixer_reads_configured_volume(self):
        calls = []
        with mock.patch.dict(pipeline.SETTINGS, {"background_audio_volume": 0.03}), \
                mock.patch("background_audio.ensure_background_audio", return_value=Path("bg.m4a")), \
                mock.patch.object(pipeline, "duration", return_value=10.0), \
                mock.patch.object(pipeline, "run", side_effect=lambda cmd, *a, **k: calls.append(cmd)):
            pipeline.mix_background_audio(Path("n.mp3"), Path("o.mp3"))
        self.assertIn("volume=0.0300", " ".join(map(str, calls[0])))


class ThumbnailAndPromptTests(unittest.TestCase):
    def test_renderer_draws_no_card(self):
        source = inspect.getsource(image_generate.render_thumbnail)
        for banned in ("rounded_rectangle", ".rectangle(", "box ="):
            self.assertNotIn(banned, source)

    def test_packaging_rules_do_not_request_dark_area(self):
        rules = (ROOT / "config" / "youtube_packaging_rules.md").read_text(encoding="utf-8")
        self.assertNotIn("dark negative-space", rules)
        self.assertIn("naturally uncluttered, low-detail text-safe area", rules)
        for word in ("panel", "box", "card", "banner", "frame", "solid dark patch"):
            self.assertIn(word, rules)

    def test_image_prompt_does_not_reserve_dark_area(self):
        source = inspect.getsource(generate_daily)
        self.assertNotIn("reserved dark", source)
        self.assertIsNone(re.search(r"reserved dark (text )?area", source))
        self.assertIn("natural uncluttered text-safe area", source)
        self.assertIn("Do not create a panel, box, card, banner, frame, solid dark patch", source)

    def test_video_renderer_uses_background_png(self):
        self.assertIn("10_background.png", inspect.getsource(pipeline.render))


if __name__ == "__main__":
    unittest.main()


class RepairPathTests(unittest.TestCase):
    def setUp(self):
        import repair_review
        self.repair = repair_review
        self.source = inspect.getsource(repair_review)

    def test_repair_never_regenerates_story_research_or_packaging(self):
        for forbidden in ("generate_daily", "research_alignment", "outline_repair", "topic_discovery",
                          "build_metadata_files", "run_packaging"):
            self.assertNotIn(forbidden, self.source)

    def test_tts_only_runs_without_clean_narration_and_never_pays(self):
        self.assertIn("12_audio_narration.mp3", self.source)
        self.assertIn("tts_cloud_engines = lambda: []", self.source)
        self.assertIn("REPAIR_STOPPED_SAFELY", self.source)
        reuse_branch = self.source.split("if narration.exists()")[1].split("else:")[0]
        self.assertNotIn("synthesize", reuse_branch)

    def test_prompt_sanitiser_removes_dark_area_requests(self):
        old = ("A lantern lit alley at night. Reserve a clean dark negative-space area occupying approximately "
               "25-35% of the composition. 16:9 YouTube thumbnail.\n\nPIPELINE SCENE RENDER: Generate the "
               "historical scene and reserved dark text area only. Do not render letters.")
        new = self.repair.sanitize_prompt(old)
        self.assertNotIn("negative-space", new)
        self.assertNotIn("reserved dark", new)
        self.assertIn("lantern lit alley", new)
        self.assertIn("naturally uncluttered, low-detail text-safe area", new)
        self.assertIn("solid dark patch", new)

    def test_frame_analysis_detects_flat_black_patch(self):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (1280, 720), (90, 80, 70))
        ImageDraw.Draw(image).rectangle((55, 205, 535, 515), fill=(5, 8, 12))
        self.assertGreaterEqual(self.repair.analyse_frame(image)["largest_cluster"], 8)
        self.assertLess(self.repair.analyse_frame(Image.new("RGB", (1280, 720), (90, 80, 70)))["largest_cluster"], 8)


class ThumbnailRenderTest(unittest.TestCase):
    def test_render_leaves_no_dark_card(self):
        import tempfile
        from PIL import Image
        import repair_review
        base = Image.new("RGB", (1280, 720), (120, 110, 95))
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "t.jpg"
            image_generate.render_thumbnail(base, out, "أزقة طرابلس البيضاء")
            self.assertEqual(repair_review.analyse_frame(Image.open(out))["largest_cluster"], 0)
