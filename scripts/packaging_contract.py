"""Single source of truth for YouTube packaging limits, visual contract and cache fingerprints.

Image models draw the BACKGROUND ONLY. Python overlays the exact Arabic thumbnail
text afterwards. Every validator (deterministic, semantic, pipeline) imports its
limits from here so the rules cannot drift apart.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING_RULES_PATH = ROOT / "config" / "youtube_packaging_rules.md"
MASTER_RULES_PATH = ROOT / "config" / "master_production_rules.md"

PACKAGING_SCHEMA_VERSION = "packaging-v3"
THUMBNAIL_STYLE_VERSION = "illustrated-realism-background-v1"
THUMBNAIL_OVERLAY_STYLE_VERSION = "arabic-overlay-v1"

TITLE_CORE_MIN_WORDS, TITLE_CORE_MAX_WORDS = 4, 9
THUMBNAIL_TEXT_MIN_WORDS, THUMBNAIL_TEXT_MAX_WORDS = 2, 4

# Geometry of the Python text overlay (1280x720 canvas). Part of the overlay stage signature.
OVERLAY_LAYOUT = {
    "canvas": (1280, 720),
    "box": (55, 205, 535, 515),
    "max_text_size": (410, 210),
    "text_anchor": (295, 360),
    "font_sizes": (108, 52),
    "fill": (241, 233, 211, 255),
}

BRAND_STYLE_DIRECTION = (
    "Cinematic illustrated realism with subtle painterly texture, premium adult storybook quality, "
    "believable and calm; not hyperrealistic, not glossy, not travel photography."
)
BACKGROUND_ONLY_DIRECTION = (
    "BACKGROUND ONLY: a clean empty dark area is reserved for a text overlay added later. "
    "No text, no letters, no numbers, no logo, no watermark, no signage."
)

ARABIC_WORD_RE = re.compile(r"[ء-ي]+")
ARABIC_CHAR_RE = re.compile(r"[؀-ۿ]")
_NEGATION_RE = re.compile(
    r"\b(no|not|never|without|avoid|avoiding|zero|free of|don't|do not|must not|forbid|forbidden|absence)\b"
)
_TEXT_TERM_RE = re.compile(
    r"\b(text|typography|lettering|letters?|words?|captions?|labels?|titles?|calligraphy|inscriptions?|"
    r"writing|subtitles?|slogan)\b"
)
_DEMAND_RE = re.compile(
    r"\b(include|includes|including|render|renders|rendered|write|written|draw|display|displays|show|shows|"
    r"add|adds|feature|features|contain|contains|print|spell|reading|says|saying|with the exact)\b"
)
_RESERVED_SPACE_RE = re.compile(
    r"\b(empty|clear|clean|reserved|negative space|open space|quiet area|dark area|dark region|blank)\b"
)


def arabic_word_count(text: str) -> int:
    return len(ARABIC_WORD_RE.findall(text))


def title_core_word_ok(core: str) -> bool:
    return TITLE_CORE_MIN_WORDS <= arabic_word_count(core) <= TITLE_CORE_MAX_WORDS


def thumbnail_text_word_ok(text: str) -> bool:
    return THUMBNAIL_TEXT_MIN_WORDS <= arabic_word_count(text) <= THUMBNAIL_TEXT_MAX_WORDS


def prompt_requests_rendered_text(prompt: str, thumbnail_text: str = "") -> bool:
    """True if an image-model prompt asks the model to write or draw text."""
    if ARABIC_CHAR_RE.search(prompt):
        return True
    if thumbnail_text and thumbnail_text.strip() and thumbnail_text.strip() in prompt:
        return True
    for clause in re.split(r"[.;\n]+", prompt.lower()):
        if not _TEXT_TERM_RE.search(clause):
            continue
        if _NEGATION_RE.search(clause):
            continue
        if _DEMAND_RE.search(clause):
            return True
    return False


def prompt_reserves_text_space(prompt: str) -> bool:
    return bool(_RESERVED_SPACE_RE.search(prompt.lower()))


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_text_sha256(path: Path) -> str:
    return text_sha256(path.read_text(encoding="utf-8")) if path.exists() else ""


def rule_fingerprints() -> dict:
    """Fingerprints that decide whether a cached packaging result is still valid."""
    return {
        "packaging_rules_sha256": file_text_sha256(PACKAGING_RULES_PATH),
        "master_production_rules_sha256": file_text_sha256(MASTER_RULES_PATH),
        "packaging_schema_version": PACKAGING_SCHEMA_VERSION,
        "thumbnail_style_version": THUMBNAIL_STYLE_VERSION,
        "thumbnail_overlay_style_version": THUMBNAIL_OVERLAY_STYLE_VERSION,
    }


def provider_prompt(original: str, max_chars: int | None = None) -> str:
    """The one canonical image prompt every provider receives."""
    parts = [original.strip()]
    for extra in (BRAND_STYLE_DIRECTION, BACKGROUND_ONLY_DIRECTION):
        if extra.lower() not in parts[0].lower():
            candidate = " ".join([*parts, extra])
            if max_chars is None or len(candidate) <= max_chars:
                parts.append(extra)
    return " ".join(parts)
