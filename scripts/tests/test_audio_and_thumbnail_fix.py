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
