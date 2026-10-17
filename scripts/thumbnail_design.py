"""Structured thumbnail design for anti-repetition and historical variety.

The design fields (scene_type, focal_subject, composition, lighting, environment)
are chosen BEFORE the thumbnail prompt is written, from the topic, angle,
historical setting and the last ten published designs. The chosen design is
injected into the image prompt and stored in metadata and the archive index.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESIGN_KEYS = ("scene_type", "focal_subject", "composition", "lighting", "environment")
METADATA_KEYS = {key: f"thumbnail_{key}" for key in DESIGN_KEYS}


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


# scene -> (focal subject candidates, default environment, relevance keywords, requires keywords)
SCENE_PROFILES: dict[str, dict] = {
    "market": {
        "focals": ["spice_merchant_stall", "cloth_stall_awning", "lantern_hung_stall"],
        "environment": "urban_dense",
        "keywords": ["market", "souk", "suq", "trade", "merchant", "سوق", "أسواق", "تاجر", "تجار", "تجارة"],
    },
    "bazaar": {
        "focals": ["covered_bazaar_arcade", "coppersmith_corner", "lamp_seller_nook"],
        "environment": "urban_dense",
        "keywords": ["bazaar", "بازار", "خان", "قيصرية"],
    },
    "caravan_camp": {
        "focals": ["resting_camel_by_fire", "loaded_pack_animals", "tent_and_fire"],
        "environment": "desert",
        "keywords": ["desert", "caravan", "oasis", "silk road", "صحراء", "قافلة", "قوافل", "واحة", "طريق الحرير", "بادية"],
    },
    "caravanserai": {
        "focals": ["inn_courtyard_well", "stable_arcade", "gatehouse_lamp"],
        "environment": "desert",
        "keywords": ["caravanserai", "inn", "khan", "نزل", "استراحة"],
    },
    "harbor": {
        "focals": ["moored_wooden_boat", "dockside_lantern", "net_mending_corner"],
        "environment": "coastal",
        "keywords": ["coast", "harbor", "harbour", "port", "sea", "island", "ميناء", "مرفأ", "ساحل", "بحر", "جزيرة", "بحار"],
        "requires": ["coast", "harbor", "harbour", "port", "sea", "island", "venice", "ميناء", "مرفأ", "ساحل", "بحر", "جزيرة", "البندقية"],
    },
    "canal": {
        "focals": ["stone_bridge_over_water", "moored_skiff", "reflecting_lantern"],
        "environment": "riverine",
        "keywords": ["river", "water", "canal", "irrigation", "نهر", "قناة", "قنوات", "ري", "ماء", "مياه", "بحيرة"],
        "requires": ["river", "water", "canal", "irrigation", "lake", "venice", "نهر", "قناة", "قنوات", "ري", "ماء", "مياه", "بحيرة", "البندقية", "الفرات", "النيل", "دجلة"],
    },
    "library": {
        "focals": ["manuscript_desk_by_candle", "scroll_shelves", "copyist_corner"],
        "environment": "architectural",
        "keywords": ["library", "book", "scholar", "manuscript", "madrasa", "مكتبة", "كتب", "علماء", "عالم", "مخطوط", "مدرسة", "وراق"],
    },
    "palace": {
        "focals": ["lamplit_audience_hall", "colonnaded_terrace", "garden_pavilion"],
        "environment": "architectural",
        "keywords": ["palace", "royal", "court", "caliph", "قصر", "ملك", "بلاط", "خليفة", "سلطان", "أمير"],
    },
    "mosque": {
        "focals": ["lamplit_prayer_hall_arches", "courtyard_fountain_at_dusk", "minaret_silhouette"],
        "environment": "urban_dense",
        "keywords": ["mosque", "minaret", "مسجد", "جامع", "مئذنة"],
        "forbidden_context": ["roman", "greek", "hellenistic", "pharaonic", "pre-islamic", "byzantine",
                              "روماني", "الرومان", "رومانية", "يوناني", "هلنستي", "فرعوني", "قبل الإسلام", "جاهلي", "بيزنطي"],
    },
    "workshop": {
        "focals": ["potter_wheel_by_lamp", "weaver_loom", "metalsmith_bench"],
        "environment": "urban_dense",
        "keywords": ["workshop", "craft", "artisan", "ورشة", "حرفي", "حرفة", "صانع", "صناع", "نساج", "فخار"],
    },
    "gate": {
        "focals": ["city_gate_lantern", "watchman_post", "arched_gateway"],
        "environment": "urban_sparse",
        "keywords": ["gate", "wall", "بوابة", "باب", "سور", "أسوار"],
    },
    "fortress": {
        "focals": ["torchlit_rampart", "watchtower_stair", "inner_ward"],
        "environment": "architectural",
        "keywords": ["fortress", "castle", "citadel", "حصن", "قلعة", "قلاع", "ثكنة"],
    },
    "garden": {
        "focals": ["orchard_path", "water_channel_terrace", "pavilion_under_trees"],
        "environment": "agricultural",
        "keywords": ["garden", "orchard", "farm", "حديقة", "بستان", "حدائق", "مزارع", "زراعة"],
    },
    "courtyard": {
        "focals": ["courtyard_well", "tiled_fountain", "arcade_corner"],
        "environment": "urban_dense",
        "keywords": ["courtyard", "house", "home", "فناء", "بيت", "منزل", "دار", "حارة"],
    },
    "narrow_street": {
        "focals": ["lamplit_alley", "doorway_steps", "wall_niche_lantern"],
        "environment": "urban_dense",
        "keywords": ["street", "alley", "quarter", "زقاق", "أزقة", "شارع", "حي", "أحياء"],
    },
    "rooftop": {
        "focals": ["terrace_sleepers", "dovecote", "skyline_chimneys"],
        "environment": "urban_sparse",
        "keywords": ["rooftop", "terrace", "سطح", "أسطح", "شرفة"],
    },
    "village_square": {
        "focals": ["communal_well", "threshing_floor", "olive_press"],
        "environment": "rural",
        "keywords": ["village", "rural", "قرية", "ريف", "قرى"],
    },
}
GENERIC_PROFILE = {"focals": ["architectural_detail", "lamplit_doorway", "quiet_corner"], "environment": "architectural"}


def _profile(scene: str) -> dict:
    return SCENE_PROFILES.get(scene, GENERIC_PROFILE)


def get_thumbnail_design_fields(metadata: dict) -> dict:
    """Extract design fields from episode/archive metadata, ignoring missing ones."""
    design = {key: metadata.get(field, "") for key, field in METADATA_KEYS.items()}
    return {key: value for key, value in design.items() if value}


def design_to_metadata(design: dict) -> dict:
    return {METADATA_KEYS[key]: design[key] for key in DESIGN_KEYS if design.get(key)}


def normalize_design(design: dict) -> dict:
    """Drop values outside the controlled vocabulary (legacy records stored junk)."""
    vocab = {
        "scene_type": set(scene_types()),
        "composition": set(composition_styles()),
        "lighting": set(lighting_moods()),
        "environment": set(environment_contexts()),
    }
    clean = {}
    for key, value in (design or {}).items():
        if key not in DESIGN_KEYS or not value:
            continue
        if key in vocab and value not in vocab[key]:
            continue
        clean[key] = value
    return clean


def recent_designs_from_records(records: list[dict], count: int = 10) -> list[dict]:
    """Designs of the most recent published records (newest first); missing fields are skipped."""
    designs = []
    for record in records:
        design = normalize_design(get_thumbnail_design_fields(record))
        if design:
            designs.append(design)
        if len(designs) >= count:
            break
    return designs


def read_recent_designs(count: int = 10, history_path: Path | None = None) -> list[dict]:
    """Read design info from recent published episodes in a topics-history file."""
    path = history_path or ROOT / "config" / "topics_history.json"
    if not path.exists():
        return []
    try:
        topics = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    published = [topic for topic in reversed(topics) if str(topic.get("status", "")).lower() == "published"]
    return recent_designs_from_records(published, count)


def should_avoid_design(proposed: dict, recent: list[dict], threshold: int = 2) -> bool:
    """True if the scene+focal+composition combo already appears `threshold` times in `recent`."""
    if not proposed or not recent:
        return False
    proposed_tuple = (proposed.get("scene_type"), proposed.get("focal_subject"), proposed.get("composition"))
    if not any(proposed_tuple):
        return False
    count = 0
    for design in recent:
        if proposed_tuple == (design.get("scene_type"), design.get("focal_subject"), design.get("composition")):
            count += 1
            if count >= threshold:
                return True
    return False


def design_conflicts(design: dict, historical_text: str) -> list[str]:
    """Reasons the design is historically implausible for the setting (empty = compatible)."""
    text = (historical_text or "").lower()
    profile = SCENE_PROFILES.get(design.get("scene_type", ""), {})
    conflicts = []
    for marker in profile.get("forbidden_context", []):
        if marker.lower() in text:
            conflicts.append(f"{design['scene_type']} is anachronistic for setting marker '{marker}'")
            break
    required = profile.get("requires")
    if required and not any(marker.lower() in text for marker in required):
        conflicts.append(f"{design['scene_type']} needs a setting that mentions {required[0]}-type geography")
    if design.get("environment") == "coastal" and design.get("scene_type") not in {"harbor", "village_square", "fortress", "market"} \
            and not any(marker.lower() in text for marker in (SCENE_PROFILES["harbor"]["requires"])):
        conflicts.append("coastal environment without a coastal setting")
    return conflicts


def _scene_relevance(text: str) -> list[str]:
    """Scenes ordered by how many of their keywords occur in the text."""
    lowered = text.lower()
    scored = []
    for order, (scene, profile) in enumerate(SCENE_PROFILES.items()):
        hits = sum(1 for keyword in profile.get("keywords", []) if keyword.lower() in lowered)
        if hits:
            scored.append((-hits, order, scene))
    return [scene for _, _, scene in sorted(scored)]


def _distinct(candidate: dict, recent: list[dict]) -> bool:
    """No repeat of the exact combo ever, and no repeat of the scene in the last three designs."""
    last_scenes = [design.get("scene_type") for design in recent[:3]]
    if candidate["scene_type"] in last_scenes:
        return False
    return not should_avoid_design(candidate, recent, threshold=1)


def choose_design(topic: str, angle: str, historical_setting: str, recent: list[dict]) -> dict:
    """Pick a historically plausible design that does not repeat recent episodes."""
    context = " ".join(part for part in (topic, angle, historical_setting) if part)
    relevant = _scene_relevance(context)
    fallback_scenes = [scene for scene in ("courtyard", "narrow_street", "rooftop", "garden", "room") if scene not in relevant]
    scenes = relevant + fallback_scenes + [s for s in scene_types() if s not in relevant and s not in fallback_scenes]
    recent_compositions = [design.get("composition") for design in recent[:3]]
    recent_lighting = [design.get("lighting") for design in recent[:2]]
    recent_focals = [design.get("focal_subject") for design in recent[:5]]
    compositions = [c for c in composition_styles() if c not in recent_compositions] or composition_styles()
    lightings = [light for light in lighting_moods() if light not in recent_lighting and light != "sunset_warm"] or lighting_moods()

    best: dict | None = None
    for scene in scenes:
        profile = _profile(scene)
        focals = [f for f in profile["focals"] if f not in recent_focals] or profile["focals"]
        for focal in focals:
            for composition in compositions:
                candidate = {
                    "scene_type": scene,
                    "focal_subject": focal,
                    "composition": composition,
                    "lighting": lightings[0],
                    "environment": profile["environment"],
                }
                if design_conflicts(candidate, context):
                    continue
                if _distinct(candidate, recent):
                    return candidate
                if best is None:
                    best = candidate
    return best or {
        "scene_type": "courtyard", "focal_subject": "courtyard_well", "composition": compositions[0],
        "lighting": lightings[0], "environment": "urban_dense",
    }


def get_distinct_design_suggestion(
    historical_setting: str,
    recent_designs: list[dict],
    family: str = "",
    topic: str = "",
    angle: str = "",
) -> dict:
    """Suggest distinct design fields for a new episode (historical accuracy + visual variety)."""
    return choose_design(topic, angle, historical_setting, list(recent_designs or []))


def design_prompt_clause(design: dict) -> str:
    """English sentence that injects the chosen design into the image prompt."""
    readable = {key: str(value).replace("_", " ") for key, value in design.items() if value}
    if not readable:
        return ""
    parts = []
    if "scene_type" in readable:
        parts.append(f"scene: {readable['scene_type']}")
    if "focal_subject" in readable:
        parts.append(f"single focal subject: {readable['focal_subject']}")
    if "composition" in readable:
        parts.append(f"composition: {readable['composition']}")
    if "lighting" in readable:
        parts.append(f"lighting: {readable['lighting']}")
    if "environment" in readable:
        parts.append(f"environment: {readable['environment']}")
    return "Design direction - " + "; ".join(parts) + "."
