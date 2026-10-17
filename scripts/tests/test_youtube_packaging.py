from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, features

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_daily
import image_generate


class YoutubePackagingTests(unittest.TestCase):
    def test_generates_valid_structured_package_and_reuses_checkpoint(self) -> None:
        topic = {
            "topic_ar": "الحياة اليومية في بغداد العباسية",
            "angle_ar": "ليلة داخل حي هادئ",
            "location_ar": "بغداد",
            "period_ar": "العصر العباسي",
            "family": "place-immersion",
        }
        package = {
            "episode_title_core": "ليلة هادئة في بغداد العباسية",
            "title_suffix": "رحلة هادئة إلى الماضي 😴",
            "final_youtube_title": "ليلة هادئة في بغداد العباسية | رحلة هادئة إلى الماضي 😴",
            "thumbnail_text": "بغداد ليلًا",
            "thumbnail_prompt": (
                "A night scene of Baghdad marketplace with warm lanterns and historic architecture. "
                "Background only, no text overlay. 16:9 YouTube thumbnail. Professional quality."
            ),
        }
        validation = {
            "status": "PASS", "title": True, "thumbnail_text": True,
            "thumbnail_prompt": True, "failing_fields": [], "issues": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story"
            (story / "logs").mkdir(parents=True)
            archive = root / "archive_index.json"
            archive.write_text("[]", encoding="utf-8")
            with mock.patch.object(generate_daily, "ARCHIVE_INDEX_PATH", archive), \
                    mock.patch.object(
                        generate_daily, "gemini",
                        side_effect=[
                            (json.dumps(package, ensure_ascii=False), []),
                            (json.dumps(validation, ensure_ascii=False), []),
                        ],
                    ) as gemini:
                result = generate_daily.generate_youtube_packaging(story, topic)
                cached = generate_daily.generate_youtube_packaging(story, topic)
            # The suffix and joined title are chosen by code, never by the model.
            suffix = generate_daily.choose_title_suffix(topic, [])
            expected = dict(package, title_suffix=suffix,
                            final_youtube_title=f"{package['episode_title_core']} | {suffix}")
            self.assertEqual(result, expected)
            self.assertEqual(cached, expected)
            self.assertEqual(gemini.call_count, 2)

    def test_thumbnail_renders_exact_arabic_label_at_mobile_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "thumbnail.jpg"
            image_generate.render_thumbnail(
                Image.new("RGB", (1920, 1080), (20, 30, 45)), output, "بغداد ليلًا"
            )
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (1280, 720))
            self.assertGreater(output.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
