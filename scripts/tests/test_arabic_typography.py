"""Pango/HarfBuzz Arabic typography for thumbnails (Pillow only composites)."""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageChops, ImageStat

import arabic_typography as typo
import image_generate

HAVE_PANGO = typo.pango_available()
try:
    FAMILY, _FILE = typo.select_arabic_font()
except typo.TypographyError:
    FAMILY = ""
needs_native = unittest.skipUnless(HAVE_PANGO and FAMILY, "Pango and an Arabic font are required (CI installs fonts-noto-core)")


def solid(color, size=(1280, 720)):
    return Image.new("RGB", size, color)


class FontSelectionTests(unittest.TestCase):
    def test_primary_font_is_noto_kufi_arabic_bold(self):
        family, path = typo.select_arabic_font(exists=lambda _p: True)
        self.assertEqual(family, "Noto Kufi Arabic")
        self.assertTrue(path.endswith("NotoKufiArabic-Bold.ttf"))

    def test_fallback_is_noto_sans_arabic_bold_then_any_arabic_font(self):
        only_sans = lambda p: str(p).endswith("NotoSansArabic-Bold.ttf")
        self.assertEqual(typo.select_arabic_font(exists=only_sans)[0], "Noto Sans Arabic")
        family, path = typo.select_arabic_font(exists=lambda _p: False, discover=lambda: ("Some Arabic Font", "/x/a.ttf"))
        self.assertEqual((family, path), ("Some Arabic Font", "/x/a.ttf"))
        with self.assertRaisesRegex(typo.TypographyError, "no Arabic-capable bold font"):
            typo.select_arabic_font(exists=lambda _p: False, discover=lambda: None)

    def test_dejavu_is_never_preferred_over_noto(self):
        self.assertNotIn("dejavu", " ".join(path for _family, path in typo.FONT_CANDIDATES).lower())
        self.assertNotIn("DejaVu", inspect.getsource(image_generate))
        self.assertFalse(hasattr(image_generate, "_thumbnail_font"))


class EngineTests(unittest.TestCase):
    def test_pillow_imagedraw_text_is_not_the_typography_engine(self):
        source = inspect.getsource(typo)
        self.assertNotIn("ImageDraw", source)
        self.assertNotIn(".text(", source)
        self.assertNotIn("rounded_rectangle", source)
        self.assertIn("pango_cairo_show_layout", source)
        render_source = inspect.getsource(image_generate.render_thumbnail)
        self.assertNotIn("draw.text", render_source)
        self.assertNotIn("rounded_rectangle", render_source)


@needs_native
class RenderingTests(unittest.TestCase):
    def test_rtl_is_native(self):
        self.assertTrue(typo.base_direction_is_rtl("سرّ ورق سمرقند"))
        # First logical letter (beh, wide) sits on the right, alef (thin) on the left.
        layer = typo.draw_text_mask("ب ا", FAMILY, 120, (100, 100, 500, 400))
        x0, _y0, x1, _y1 = layer.bbox
        mid = (x0 + x1) // 2
        left = ImageStat.Stat(layer.mask.crop((x0, 0, mid, 720))).sum[0]
        right = ImageStat.Stat(layer.mask.crop((mid, 0, x1, 720))).sum[0]
        self.assertGreater(right, left)

    def test_connected_arabic_glyphs_are_shaped(self):
        word = "سمرقند"
        joined = typo.measure(word, FAMILY, 100, 2000)[0].width
        isolated = sum(typo.measure(letter, FAMILY, 100, 2000)[0].width for letter in word)
        self.assertLess(joined, isolated)  # joining forms are narrower than isolated letters
        lam_alef = typo.measure("لا", FAMILY, 100, 2000)[0].width
        self.assertLess(lam_alef, typo.measure("ل", FAMILY, 100, 2000)[0].width + typo.measure("ا", FAMILY, 100, 2000)[0].width)

    def test_two_word_and_four_word_phrases_pass(self):
        for phrase in ("ورق سمرقند", "كيف صنعوا ورق سمرقند"):
            final, report = typo.compose_thumbnail(solid((16, 24, 40)), phrase)
            self.assertEqual(final.size, (1280, 720))
            self.assertEqual(report["engine"], "pango/harfbuzz")
            self.assertEqual(report["font_family"], FAMILY)

    def test_automatic_two_line_wrapping(self):
        lines = typo.measure("كيف صنعوا الورق في سمرقند", FAMILY, 110, 540)[2]
        self.assertGreaterEqual(lines, 2)
        _final, report = typo.compose_thumbnail(solid((16, 24, 40)), "كيف صنعوا الورق في سمرقند")
        self.assertLessEqual(report["lines"], 2)
        with self.assertRaisesRegex(typo.TypographyError, "THUMBNAIL_TYPOGRAPHY_FAILED"):
            typo.fit_text(" ".join(["سمرقندية"] * 12), FAMILY, typo.REGIONS["right"])

    def test_no_black_card_and_adaptive_scrim_only_when_needed(self):
        dark = solid((14, 20, 34))
        final_dark, report_dark = typo.compose_thumbnail(dark, "سر ورق سمرقند")
        self.assertFalse(report_dark["scrim"])
        bright = solid((205, 190, 160))
        final_bright, report_bright = typo.compose_thumbnail(bright, "سر ورق سمرقند")
        self.assertTrue(report_bright["scrim"])
        # Far from the text the picture is untouched, in both cases (no UI card, no border).
        far = (0, 0, 300, 120)
        for final, base in ((final_dark, dark), (final_bright, bright)):
            diff = ImageChops.difference(final.convert("RGB").crop(far), base.crop(far))
            self.assertEqual(diff.getbbox(), None)
        # The bright image is darkened softly behind the text: strongest at the text, fading outward.
        region = typo.REGIONS[report_bright["region"]]
        cx, cy = (region[0] + region[2]) // 2, (region[1] + region[3]) // 2
        at_text = sum(final_bright.convert("RGB").getpixel((cx, cy - 190)))
        farther = sum(final_bright.convert("RGB").getpixel((min(1270, cx + 330), cy - 190)))
        self.assertLess(at_text, farther)

    def test_readable_at_320x180_and_inside_safe_margins(self):
        for color in ((14, 20, 34), (205, 190, 160)):
            _final, report = typo.compose_thumbnail(solid(color), "من التوت إلى ورق")
            self.assertGreaterEqual(report["contrast_320x180"], typo.MIN_MOBILE_CONTRAST)
            self.assertGreaterEqual(report["line_height_320x180"], typo.MIN_MOBILE_LINE_PX)
            self.assertTrue(report["inside_safe_margins"])
            self.assertTrue(report["clear_of_duration_badge"])

    def test_unreadable_text_fails_typography_validation(self):
        ivory = typo.TEXT_COLOR
        layer = typo.draw_text_mask("سر ورق سمرقند", FAMILY, 100, typo.REGIONS["right"])
        flat = Image.new("RGBA", (1280, 720), (*ivory, 255))
        with self.assertRaisesRegex(typo.TypographyError, "THUMBNAIL_TYPOGRAPHY_FAILED"):
            typo.validate_mobile_typography(flat, layer)

    def test_text_outside_margins_or_under_badge_fails(self):
        layer = typo.draw_text_mask("سر ورق سمرقند", FAMILY, 100, typo.REGIONS["right"])
        edge = typo.TextLayer(layer.mask, (5, 100, 300, 300), layer.lines, layer.size_px, layer.family, True)
        final = Image.new("RGBA", (1280, 720), (10, 10, 20, 255))
        final.paste(Image.new("RGBA", (300, 200), (*typo.TEXT_COLOR, 255)), (5, 100))
        with self.assertRaisesRegex(typo.TypographyError, "safe margins"):
            typo.validate_mobile_typography(final, edge)
        badge = typo.TextLayer(layer.mask, (1100, 650, 1250, 700), layer.lines, layer.size_px, layer.family, True)
        with self.assertRaisesRegex(typo.TypographyError, "duration badge"):
            typo.validate_mobile_typography(final, badge)

    def test_region_choice_prefers_the_calmer_darker_side(self):
        canvas = solid((14, 20, 34)).convert("RGBA")
        canvas.paste(Image.effect_noise((560, 450), 90).convert("RGBA"), (typo.REGIONS["right"][0], 130))
        name, _stats, _all = typo.choose_region(canvas)
        self.assertEqual(name, "left")

    def test_render_thumbnail_keeps_the_exact_text_and_writes_a_jpeg(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "t.jpg"
            with mock.patch.object(typo, "draw_text_mask", wraps=typo.draw_text_mask) as draw:
                report = image_generate.render_thumbnail(solid((14, 20, 34)), out, "سرّ ورق سمرقند")
            self.assertEqual(draw.call_args.args[0], "سرّ ورق سمرقند")  # exact thumbnail_text
            self.assertEqual(Image.open(out).size, (1280, 720))
            self.assertEqual(report["engine"], "pango/harfbuzz")


class IntegrationBoundaryTests(unittest.TestCase):
    def test_word_limits_are_two_to_five(self):
        for bad in ("كلمة", "واحد اثنان ثلاثة اربعة خمسة ستة"):
            with self.assertRaisesRegex(RuntimeError, "2-5 Arabic words"):
                image_generate.render_thumbnail(solid((0, 0, 0)), Path("x.jpg"), bad)

    def test_typography_runs_after_the_background_and_never_reaches_the_image_model(self):
        source = inspect.getsource(image_generate.generate)
        self.assertLess(source.index("generate_cloudflare(original_prompt"), source.index("render_thumbnail(image"))
        self.assertNotIn("thumbnail_text", source[: source.index("render_thumbnail(image")].split("original_prompt = ")[1].split("seed =")[0])
        self.assertIn('"thumbnail_typography": typography_report', source)


if __name__ == "__main__":
    unittest.main()
