"""Arabic thumbnail typography: Pango/HarfBuzz draws the text, Pillow only composites.

Pango (through pangocairo) does the Arabic shaping, bidi/RTL, ligatures, glyph
positioning and line layout. Pillow never draws a glyph here; it blends layers,
blurs masks, resizes and saves. The native libraries are reached with ``ctypes``
so no Python extension has to be compiled.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

CANVAS_W, CANVAS_H = 1280, 720
MARGIN = 72
PANGO_SCALE = 1024
PANGO_WEIGHT_BOLD = 700
PANGO_ALIGN_CENTER = 1
PANGO_WRAP_WORD = 0
PANGO_DIRECTION_RTL = 1

TEXT_COLOR = (243, 235, 214)  # warm ivory / off-white
SHADOW_COLOR = (4, 8, 16)
MAX_LINES = 2
START_SIZE_PX, MIN_SIZE_PX = 150, 60

# Primary font first; DejaVu is never preferred over a Noto Arabic face.
FONT_CANDIDATES = (
    ("Noto Kufi Arabic", "/usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf"),
    ("Noto Sans Arabic", "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"),
)

# Candidate text areas on the 1280x720 canvas. The bottom-right corner is kept
# free of text because YouTube draws its duration badge there.
REGIONS = {
    "left": (MARGIN, 130, 640 - 24, 580),
    "right": (640 + 24, 130, CANVAS_W - MARGIN, 580),
}
DURATION_BADGE = (CANVAS_W - 230, CANVAS_H - 90, CANVAS_W, CANVAS_H)

MIN_MOBILE_CONTRAST = 3.0  # WCAG large-text contrast, measured at 320x180
MIN_MOBILE_LINE_PX = 14  # smallest allowed line height at 320x180


class TypographyError(RuntimeError):
    pass


# --------------------------------------------------------------------------- fonts


def _fontconfig_arabic_bold() -> tuple[str, str] | None:
    try:
        out = subprocess.run(
            ["fc-list", ":lang=ar:weight=bold", "family", "file"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if ":" in line:
            path, _, family = line.partition(":")
            family = family.strip().split(",")[0].strip()
            if family and path.strip():
                return family, path.strip()
    return None


def select_arabic_font(exists=Path.exists, discover=_fontconfig_arabic_bold) -> tuple[str, str]:
    """Return (family, file). Noto Kufi Arabic Bold, then Noto Sans Arabic Bold,
    then any Arabic-capable bold system font."""
    for family, path in FONT_CANDIDATES:
        if exists(Path(path)):
            return family, path
    found = discover()
    if found:
        return found
    raise TypographyError("THUMBNAIL_TEXT_FAILED: no Arabic-capable bold font found (install fonts-noto-core)")


# ---------------------------------------------------------------------- native libs


class _Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int)]


_LIBS: dict | None = None


def _load(names: tuple[str, ...]) -> ctypes.CDLL:
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise TypographyError(f"THUMBNAIL_TEXT_FAILED: native library not found: {names[0]}")


def _libs() -> dict:
    global _LIBS
    if _LIBS is not None:
        return _LIBS
    cairo = _load(("libcairo.so.2", ctypes.util.find_library("cairo") or "libcairo.so.2"))
    pango = _load(("libpango-1.0.so.0", ctypes.util.find_library("pango-1.0") or "libpango-1.0.so.0"))
    pangocairo = _load(("libpangocairo-1.0.so.0", ctypes.util.find_library("pangocairo-1.0") or "libpangocairo-1.0.so.0"))
    gobject = _load(("libgobject-2.0.so.0", ctypes.util.find_library("gobject-2.0") or "libgobject-2.0.so.0"))
    vp, ci, cd, cs = ctypes.c_void_p, ctypes.c_int, ctypes.c_double, ctypes.c_char_p
    for lib, name, res, args in (
        (cairo, "cairo_image_surface_create", vp, [ci, ci, ci]),
        (cairo, "cairo_create", vp, [vp]),
        (cairo, "cairo_destroy", None, [vp]),
        (cairo, "cairo_surface_destroy", None, [vp]),
        (cairo, "cairo_surface_flush", None, [vp]),
        (cairo, "cairo_image_surface_get_data", vp, [vp]),
        (cairo, "cairo_image_surface_get_stride", ci, [vp]),
        (cairo, "cairo_set_source_rgba", None, [vp, cd, cd, cd, cd]),
        (cairo, "cairo_move_to", None, [vp, cd, cd]),
        (pangocairo, "pango_cairo_create_layout", vp, [vp]),
        (pangocairo, "pango_cairo_show_layout", None, [vp, vp]),
        (pango, "pango_font_description_new", vp, []),
        (pango, "pango_font_description_set_family", None, [vp, cs]),
        (pango, "pango_font_description_set_weight", None, [vp, ci]),
        (pango, "pango_font_description_set_absolute_size", None, [vp, cd]),
        (pango, "pango_font_description_free", None, [vp]),
        (pango, "pango_layout_set_font_description", None, [vp, vp]),
        (pango, "pango_layout_set_text", None, [vp, cs, ci]),
        (pango, "pango_layout_set_width", None, [vp, ci]),
        (pango, "pango_layout_set_wrap", None, [vp, ci]),
        (pango, "pango_layout_set_alignment", None, [vp, ci]),
        (pango, "pango_layout_get_line_count", ci, [vp]),
        (pango, "pango_layout_get_pixel_extents", None, [vp, ctypes.POINTER(_Rect), ctypes.POINTER(_Rect)]),
        (pango, "pango_find_base_dir", ci, [cs, ci]),
        (gobject, "g_object_unref", None, [vp]),
    ):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args
    try:
        pango.pango_layout_set_line_spacing.restype = None
        pango.pango_layout_set_line_spacing.argtypes = [vp, ctypes.c_float]
    except AttributeError:  # Pango < 1.44: default spacing
        pass
    _LIBS = {"cairo": cairo, "pango": pango, "pangocairo": pangocairo, "gobject": gobject}
    return _LIBS


def pango_available() -> bool:
    try:
        _libs()
        return True
    except (TypographyError, AttributeError):
        return False


# ----------------------------------------------------------------------- text layer


@dataclass
class TextLayer:
    mask: Image.Image  # "L" alpha of the text, canvas-sized
    bbox: tuple[int, int, int, int]  # ink bounding box on the canvas
    lines: int
    size_px: int
    family: str
    direction_rtl: bool


def base_direction_is_rtl(text: str) -> bool:
    return _libs()["pango"].pango_find_base_dir(text.encode("utf-8"), -1) == PANGO_DIRECTION_RTL


def _make_layout(cr, text: str, family: str, size_px: int, width_px: int, line_spacing: float):
    libs = _libs()
    layout = libs["pangocairo"].pango_cairo_create_layout(cr)
    description = libs["pango"].pango_font_description_new()
    libs["pango"].pango_font_description_set_family(description, family.encode("utf-8"))
    libs["pango"].pango_font_description_set_weight(description, PANGO_WEIGHT_BOLD)
    libs["pango"].pango_font_description_set_absolute_size(description, size_px * PANGO_SCALE)
    libs["pango"].pango_layout_set_font_description(layout, description)
    libs["pango"].pango_font_description_free(description)
    libs["pango"].pango_layout_set_text(layout, text.encode("utf-8"), -1)
    libs["pango"].pango_layout_set_width(layout, int(width_px * PANGO_SCALE))
    libs["pango"].pango_layout_set_wrap(layout, PANGO_WRAP_WORD)
    libs["pango"].pango_layout_set_alignment(layout, PANGO_ALIGN_CENTER)
    if hasattr(libs["pango"], "pango_layout_set_line_spacing"):
        libs["pango"].pango_layout_set_line_spacing(layout, line_spacing)
    return layout


def _extents(layout) -> tuple[_Rect, _Rect, int]:
    libs = _libs()
    ink, logical = _Rect(), _Rect()
    libs["pango"].pango_layout_get_pixel_extents(layout, ctypes.byref(ink), ctypes.byref(logical))
    return ink, logical, libs["pango"].pango_layout_get_line_count(layout)


def measure(text: str, family: str, size_px: int, width_px: int, line_spacing: float = 1.0):
    """Lay the text out on a scratch surface; returns (ink, logical, line_count)."""
    libs = _libs()
    surface = libs["cairo"].cairo_image_surface_create(0, 8, 8)  # CAIRO_FORMAT_ARGB32
    cr = libs["cairo"].cairo_create(surface)
    try:
        layout = _make_layout(cr, text, family, size_px, width_px, line_spacing)
        try:
            return _extents(layout)
        finally:
            libs["gobject"].g_object_unref(layout)
    finally:
        libs["cairo"].cairo_destroy(cr)
        libs["cairo"].cairo_surface_destroy(surface)


def draw_text_mask(
    text: str, family: str, size_px: int, region: tuple[int, int, int, int],
    *, line_spacing: float = 1.0,
) -> TextLayer:
    """Draw ``text`` with Pango into a canvas-sized alpha mask, centred in ``region``."""
    libs = _libs()
    x0, y0, x1, y1 = region
    width = x1 - x0
    ink, logical, lines = measure(text, family, size_px, width, line_spacing)
    surface = libs["cairo"].cairo_image_surface_create(0, CANVAS_W, CANVAS_H)
    cr = libs["cairo"].cairo_create(surface)
    try:
        layout = _make_layout(cr, text, family, size_px, width, line_spacing)
        try:
            offset_x = x0 + (width - logical.width) / 2 - logical.x
            offset_y = y0 + ((y1 - y0) - logical.height) / 2 - logical.y
            libs["cairo"].cairo_move_to(cr, offset_x, offset_y)
            libs["cairo"].cairo_set_source_rgba(cr, 1.0, 1.0, 1.0, 1.0)
            libs["pangocairo"].pango_cairo_show_layout(cr, layout)
        finally:
            libs["gobject"].g_object_unref(layout)
        libs["cairo"].cairo_surface_flush(surface)
        stride = libs["cairo"].cairo_image_surface_get_stride(surface)
        data = libs["cairo"].cairo_image_surface_get_data(surface)
        raw = ctypes.string_at(data, stride * CANVAS_H)
        rgba = Image.frombuffer("RGBA", (CANVAS_W, CANVAS_H), raw, "raw", "BGRa", stride, 1)
        mask = rgba.getchannel("A").copy()
    finally:
        libs["cairo"].cairo_destroy(cr)
        libs["cairo"].cairo_surface_destroy(surface)
    bbox = mask.getbbox()
    if bbox is None:
        raise TypographyError("THUMBNAIL_TEXT_FAILED: Pango rendered no glyphs")
    return TextLayer(mask, bbox, lines, size_px, family, base_direction_is_rtl(text))


def fit_text(text: str, family: str, region: tuple[int, int, int, int]) -> tuple[int, int]:
    """Largest size (px) that keeps the text in <= MAX_LINES lines inside ``region``."""
    x0, y0, x1, y1 = region
    for size in range(START_SIZE_PX, MIN_SIZE_PX - 1, -4):
        ink, logical, lines = measure(text, family, size, x1 - x0)
        if lines <= MAX_LINES and logical.width <= (x1 - x0) and logical.height <= (y1 - y0):
            return size, lines
    raise TypographyError(
        f"THUMBNAIL_TYPOGRAPHY_FAILED: text does not fit in {MAX_LINES} lines at >= {MIN_SIZE_PX}px"
    )


# ------------------------------------------------------------------ region analysis


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def analyse_region(canvas: Image.Image, box: tuple[int, int, int, int]) -> dict:
    """Light, deterministic measures: mean/bright luminance and visual density."""
    crop = canvas.convert("RGB").crop(box)
    stat = ImageStat.Stat(crop)
    mean_lum = _luminance(tuple(stat.mean))
    gray = crop.convert("L")
    histogram = gray.histogram()
    target, running, p90 = sum(histogram) * 0.90, 0, 255
    for level, count in enumerate(histogram):
        running += count
        if running >= target:
            p90 = level
            break
    bright_lum = _luminance((p90, p90, p90))
    edges = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]
    text_lum = _luminance(TEXT_COLOR)
    contrast = contrast_ratio(text_lum, mean_lum)
    contrast_bright = contrast_ratio(text_lum, bright_lum)
    calm = 1.0 - min(1.0, edges / 30.0)
    score = 0.5 * min(contrast / 10.0, 1.0) + 0.5 * calm
    return {
        "mean_luminance": mean_lum, "bright_luminance": bright_lum, "edge_density": edges,
        "contrast": contrast, "contrast_bright": contrast_bright, "calm": calm, "score": score,
    }


def choose_region(canvas: Image.Image) -> tuple[str, dict, dict]:
    analyses = {name: analyse_region(canvas, box) for name, box in REGIONS.items()}
    best = max(analyses, key=lambda name: (round(analyses[name]["score"], 6), name == "right"))
    return best, analyses[best], analyses


def needs_scrim(stats: dict) -> bool:
    """A scrim is only added when ivory text would not already be easy to read."""
    return stats["contrast"] < 5.5 or stats["contrast_bright"] < 3.2


# --------------------------------------------------------------------- composition


def scrim_alpha(stats: dict) -> int:
    deficit = max(0.0, 6.5 - stats["contrast"])
    return int(max(70, min(170, 70 + deficit * 22)))


def apply_scrim(canvas: Image.Image, bbox: tuple[int, int, int, int], alpha: int) -> Image.Image:
    """Soft local gradient behind the text only: a blurred, borderless dark patch."""
    pad_x, pad_y = 90, 70
    box = (bbox[0] - pad_x, bbox[1] - pad_y, bbox[2] + pad_x, bbox[3] + pad_y)
    mask = Image.new("L", canvas.size, 0)
    mask.paste(alpha, box)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=70))
    dark = Image.new("RGBA", canvas.size, (*SHADOW_COLOR, 0))
    dark.putalpha(mask)
    return Image.alpha_composite(canvas, dark)


def apply_text(canvas: Image.Image, layer: TextLayer) -> Image.Image:
    shadow_mask = ImageChops.offset(layer.mask, 0, 4).filter(ImageFilter.GaussianBlur(radius=7))
    shadow_mask = shadow_mask.point(lambda v: int(v * 0.62))
    shadow = Image.new("RGBA", canvas.size, (*SHADOW_COLOR, 0))
    shadow.putalpha(shadow_mask)
    text = Image.new("RGBA", canvas.size, (*TEXT_COLOR, 0))
    text.putalpha(layer.mask)
    return Image.alpha_composite(Image.alpha_composite(canvas, shadow), text)


# --------------------------------------------------------------------- validation


def validate_mobile_typography(final: Image.Image, layer: TextLayer) -> dict:
    """Preview at 320x180 and fail when the phrase would not read at a glance."""
    scale = 320 / CANVAS_W
    small = final.convert("RGB").resize((320, 180), Image.Resampling.LANCZOS)
    mask = layer.mask.resize((320, 180), Image.Resampling.LANCZOS)
    ink = mask.point(lambda v: 255 if v > 128 else 0)
    ring = ImageChops.subtract(ink.filter(ImageFilter.MaxFilter(9)), ink.filter(ImageFilter.MaxFilter(3)))
    ink_pixels = ImageStat.Stat(small, mask=ink)
    ring_pixels = ImageStat.Stat(small, mask=ring)
    if ink_pixels.count[0] == 0 or ring_pixels.count[0] == 0:
        raise TypographyError("THUMBNAIL_TYPOGRAPHY_FAILED: text vanishes at 320x180")
    contrast = contrast_ratio(_luminance(tuple(ink_pixels.mean)), _luminance(tuple(ring_pixels.mean)))
    x0, y0, x1, y1 = layer.bbox
    line_px = ((y1 - y0) / layer.lines) * scale
    inside_margins = x0 >= MARGIN - 8 and x1 <= CANVAS_W - MARGIN + 8 and y0 >= 40 and y1 <= CANVAS_H - 120
    bx0, by0, bx1, by1 = DURATION_BADGE
    clear_of_badge = x1 < bx0 or y1 < by0 or x0 > bx1 or y0 > by1
    result = {
        "contrast_320x180": round(contrast, 2), "line_height_320x180": round(line_px, 1),
        "inside_safe_margins": inside_margins, "clear_of_duration_badge": clear_of_badge,
        "lines": layer.lines,
    }
    problems = []
    if contrast < MIN_MOBILE_CONTRAST:
        problems.append(f"contrast {contrast:.2f} < {MIN_MOBILE_CONTRAST} at 320x180")
    if line_px < MIN_MOBILE_LINE_PX:
        problems.append(f"line height {line_px:.1f}px < {MIN_MOBILE_LINE_PX}px at 320x180")
    if not inside_margins:
        problems.append("text leaves the safe margins")
    if not clear_of_badge:
        problems.append("text collides with the YouTube duration badge")
    if layer.lines > MAX_LINES:
        problems.append("more than two lines")
    if problems:
        raise TypographyError("THUMBNAIL_TYPOGRAPHY_FAILED: " + "; ".join(problems))
    return result


# --------------------------------------------------------------------------- entry


def compose_thumbnail(base: Image.Image, text: str) -> tuple[Image.Image, dict]:
    """Return (final RGBA canvas, report). Pango draws ``text``; Pillow composites."""
    family, font_path = select_arabic_font()
    canvas = base.resize((CANVAS_W, CANVAS_H), Image.Resampling.LANCZOS).convert("RGBA")
    region_name, stats, _ = choose_region(canvas)
    region = REGIONS[region_name]
    size, _lines = fit_text(text, family, region)
    layer = draw_text_mask(text, family, size, region)
    scrim = needs_scrim(stats)
    working = apply_scrim(canvas, layer.bbox, scrim_alpha(stats)) if scrim else canvas
    final = apply_text(working, layer)
    report = validate_mobile_typography(final, layer)
    report.update({
        "engine": "pango/harfbuzz", "font_family": family, "font_file": font_path,
        "region": region_name, "scrim": scrim, "size_px": size, "rtl": layer.direction_rtl,
    })
    return final, report
