"""Structured thumbnail design for anti-repetition and historical variety.

Tracks thumbnail scene composition across published episodes to avoid
repetitive visual patterns while maintaining historical accuracy and
mood consistency.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def get_thumbnail_design_fields(metadata: dict) -> dict:
    """Extract or compute thumbnail design fields from episode metadata."""
    design = {
        "scene_type": metadata.get("thumbnail_scene_type", ""),
        "focal_subject": metadata.get("thumbnail_focal_subject", ""),
        "composition": metadata.get("thumbnail_composition", ""),
        "lighting": metadata.get("thumbnail_lighting", ""),
        "environment": metadata.get("thumbnail_environment", ""),
    }
    return {k: v for k, v in design.items() if v}


def scene_types() -> list[str]:
    """Common historical scene types to vary across episodes."""
    return [
        "canal", "narrow_street", "courtyard", "room", "caravan_camp",
        "harbor", "library", "rooftop", "market", "bazaar", "palace",
        "workshop", "caravanserai", "mosque", "bridge", "fountain",
        "village_square", "garden", "fortress", "tower", "gate",
    ]


def composition_styles() -> list[str]:
    """Visual composition approaches."""
    return [
        "wide_establishing", "intimate_interior", "leading_lines",
        "doorway_view", "window_view", "corner_perspective",
        "overhead_angle", "ground_level", "through_archway",
        "silhouette_foreground", "centered_subject", "asymmetric_balance",
    ]


def lighting_moods() -> list[str]:
    """Lighting and time-of-day variations."""
    return [
        "moonlit", "rainy_night", "lantern_low_key", "candlelit",
        "pre_dawn", "sunset_warm", "overcast_gray", "starlit",
        "torch_lit", "firelit", "natural_window", "shadow_play",
    ]


def environment_contexts() -> list[str]:
    """Environmental/contextual variations."""
    return [
        "urban_dense", "urban_sparse", "rural", "desert", "mountains",
        "coastal", "riverine", "woodland", "agricultural", "architectural",
        "natural_landscape", "settlement",
    ]


def read_recent_designs(
    count: int = 10,
    history_path: Path | None = None,
) -> list[dict]:
    """Read design info from recent published episodes."""
    if not history_path:
        history_path = ROOT / "config" / "topics_history.json"

    if not history_path.exists():
        return []

    try:
        topics = json.loads(history_path.read_text(encoding="utf-8"))
        designs = []
        for topic in reversed(topics):
            if topic.get("status") == "published":
                design = get_thumbnail_design_fields(topic)
                if design:
                    designs.append(design)
            if len(designs) >= count:
                break
        return designs
    except Exception:
        return []


def should_avoid_design(
    proposed: dict,
    recent: list[dict],
    threshold: int = 2,
) -> bool:
    """Check if proposed design combination is too similar to recent episodes.

    Avoids repeating the same scene_type+focal_subject+composition combo
    within the last N episodes.
    """
    if not proposed or not recent:
        return False

    proposed_tuple = (
        proposed.get("scene_type"),
        proposed.get("focal_subject"),
        proposed.get("composition"),
    )

    if not any(proposed_tuple):
        return False

    count = 0
    for design in recent:
        recent_tuple = (
            design.get("scene_type"),
            design.get("focal_subject"),
            design.get("composition"),
        )
        if proposed_tuple == recent_tuple:
            count += 1
            if count >= threshold:
                return True

    return False


def get_distinct_design_suggestion(
    historical_setting: str,
    recent_designs: list[dict],
    family: str = "",
) -> dict:
    """Suggest distinct design fields for a new episode.

    Balances historical accuracy with visual variety.
    """
    suggestion = {
        "scene_type": "courtyard",
        "focal_subject": "architectural_detail",
        "composition": "intimate_interior",
        "lighting": "lantern_low_key",
        "environment": "urban_dense",
    }

    setting_lower = historical_setting.lower()

    if "market" in setting_lower or "bazaar" in setting_lower or "souk" in setting_lower:
        suggestion["scene_type"] = "market"
        suggestion["environment"] = "urban_dense"
    elif "desert" in setting_lower or "caravan" in setting_lower:
        suggestion["scene_type"] = "caravan_camp"
        suggestion["environment"] = "desert"
        suggestion["lighting"] = "moonlit"
    elif "coast" in setting_lower or "harbor" in setting_lower or "port" in setting_lower:
        suggestion["scene_type"] = "harbor"
        suggestion["environment"] = "coastal"
    elif "river" in setting_lower or "water" in setting_lower or "canal" in setting_lower:
        suggestion["scene_type"] = "canal"
        suggestion["environment"] = "riverine"
    elif "library" in setting_lower or "book" in setting_lower or "scholar" in setting_lower:
        suggestion["scene_type"] = "library"
        suggestion["composition"] = "intimate_interior"
    elif "palace" in setting_lower or "royal" in setting_lower or "court" in setting_lower:
        suggestion["scene_type"] = "palace"
        suggestion["composition"] = "wide_establishing"

    if should_avoid_design(suggestion, recent_designs):
        for scene in scene_types():
            test_design = dict(suggestion)
            test_design["scene_type"] = scene
            if not should_avoid_design(test_design, recent_designs):
                suggestion["scene_type"] = scene
                break

    return suggestion
