"""Deterministic, structure-only repair of a model-written outline.

Repairs numbers, counts, phase order and trivially missing fields. It never
writes historical content: every word it adds is either copied from the
outline itself or a neutral connective phrase.
"""

from __future__ import annotations

import re

NEUTRAL_TRANSITION = "ننتقل بهدوء إلى المشهد التالي"
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def coerce_positive_int(value: object) -> int | None:
    """``"700"`` / ``700.0`` / ``"٧٠٠ كلمة"`` -> 700; anything unusable -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(round(value))
    elif isinstance(value, str):
        match = re.search(r"\d+", value.translate(_ARABIC_DIGITS).replace(",", ""))
        number = int(match.group()) if match else 0
    else:
        return None
    return number if number > 0 else None


def _text(value: object) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _repair_scene(scene: dict, section_title: str, repairs: list[str], where: str) -> dict:
    fixed = dict(scene)
    if not any(_text(fixed.get(key)) for key in ("place_or_anchor", "main_idea", "action", "gentle_question")):
        return fixed  # an empty scene is a content problem, not a format problem
    if not _text(fixed.get("main_idea")) and _text(fixed.get("action")):
        fixed["main_idea"] = _text(fixed["action"])
        repairs.append(f"{where}: main_idea taken from action")
    if not _text(fixed.get("place_or_anchor")) and section_title:
        fixed["place_or_anchor"] = section_title
        repairs.append(f"{where}: place_or_anchor taken from the section title")
    if not _text(fixed.get("transition")):
        fixed["transition"] = NEUTRAL_TRANSITION
        repairs.append(f"{where}: neutral transition added")
    return fixed


def _positional_phases(count: int, phases: tuple[str, ...]) -> list[str]:
    last = len(phases) - 1
    chosen = [phases[min(last, index * len(phases) // count)] for index in range(count)]
    chosen[0], chosen[-1] = phases[0], phases[-1]
    return chosen


def _phases_valid(sections: list[dict], phases: tuple[str, ...]) -> bool:
    names = [_text(section.get("phase")) for section in sections]
    if any(name not in phases for name in names):
        return False
    indexes = [phases.index(name) for name in names]
    return indexes == sorted(indexes) and set(names) == set(phases) and names[0] == phases[0] and names[-1] == phases[-1]


def _split_section(section: dict) -> list[dict]:
    scenes = section["scenes"]
    half = len(scenes) // 2
    first, second = dict(section), dict(section)
    first["scenes"], second["scenes"] = scenes[:half], scenes[half:]
    total = coerce_positive_int(section.get("target_words")) or 0
    first["target_words"], second["target_words"] = total // 2, total - total // 2
    anchor = _text(scenes[half].get("place_or_anchor"))
    second["title"] = anchor or _text(section.get("title"))
    return [first, second]


def _merge_pair(left: dict, right: dict) -> dict:
    merged = dict(left)
    merged["scenes"] = list(left["scenes"]) + list(right["scenes"])
    merged["target_words"] = (coerce_positive_int(left.get("target_words")) or 0) + (
        coerce_positive_int(right.get("target_words")) or 0)
    return merged


def repair_outline(
    raw_sections: object, *, phases: tuple[str, ...], min_sections: int, max_sections: int,
    min_scenes: int, max_scenes: int, min_total_words: int, max_section_words: int = 1400,
) -> tuple[object, list[str]]:
    """Return ``(sections, repairs)``; unrepairable input is returned unchanged for the validator."""
    repairs: list[str] = []
    if not isinstance(raw_sections, list) or not raw_sections or not all(isinstance(item, dict) for item in raw_sections):
        return raw_sections, repairs
    sections = [dict(item) for item in raw_sections]

    for index, section in enumerate(sections, 1):
        title = _text(section.get("title"))
        scenes = section.get("scenes")
        if isinstance(scenes, list):
            if all(isinstance(scene, dict) for scene in scenes):
                section["scenes"] = [
                    _repair_scene(scene, title, repairs, f"section {index} scene {number}")
                    for number, scene in enumerate(scenes, 1)
                ]
            if len(section["scenes"]) > max_scenes:
                repairs.append(f"section {index}: scenes cut from {len(section['scenes'])} to {max_scenes}")
                section["scenes"] = section["scenes"][:max_scenes]
        coerced = coerce_positive_int(section.get("target_words"))
        if coerced is not None and coerced != section.get("target_words"):
            repairs.append(f"section {index}: target_words {section.get('target_words')!r} -> {coerced}")
        section["target_words"] = coerced

    def scenes_of(section: dict) -> list | None:
        scenes = section.get("scenes")
        return scenes if isinstance(scenes, list) and all(isinstance(scene, dict) for scene in scenes) else None

    # Too many sections: merge neighbours of the same phase when their scenes still fit.
    while len(sections) > max_sections:
        candidates = [
            index for index in range(len(sections) - 1)
            if _text(sections[index].get("phase")) == _text(sections[index + 1].get("phase"))
            and scenes_of(sections[index]) is not None and scenes_of(sections[index + 1]) is not None
            and len(sections[index]["scenes"]) + len(sections[index + 1]["scenes"]) <= max_scenes
        ]
        if not candidates:
            break
        pick = min(candidates, key=lambda i: (sections[i].get("target_words") or 0) + (sections[i + 1].get("target_words") or 0))
        sections[pick:pick + 2] = [_merge_pair(sections[pick], sections[pick + 1])]
        repairs.append(f"sections {pick + 1}-{pick + 2} merged")

    # Too few sections: split the largest section that can give two valid halves.
    while len(sections) < min_sections:
        splittable = [
            index for index, section in enumerate(sections)
            if scenes_of(section) is not None and len(section["scenes"]) >= 2 * min_scenes
        ]
        if not splittable:
            break
        pick = max(splittable, key=lambda i: len(sections[i]["scenes"]))
        sections[pick:pick + 1] = _split_section(sections[pick])
        repairs.append(f"section {pick + 1} split in two")

    if len(sections) >= len(phases) and not _phases_valid(sections, phases):
        for section, phase in zip(sections, _positional_phases(len(sections), phases)):
            section["phase"] = phase
        repairs.append("phases reassigned by position")

    # Targets: fill gaps, then lift the total to the minimum without touching section content.
    known = [s["target_words"] for s in sections if s["target_words"]]
    if known and len(known) < len(sections):
        average = round(sum(known) / len(known))
        for section in sections:
            if not section["target_words"]:
                section["target_words"] = average
                repairs.append("missing target_words filled with the average")
    if all(s["target_words"] for s in sections):
        total = sum(s["target_words"] for s in sections)
        if total < min_total_words:
            before = total
            for _ in range(8):
                deficit = min_total_words - sum(s["target_words"] for s in sections)
                if deficit <= 0:
                    break
                room = [s for s in sections if s["target_words"] < max_section_words]
                if not room:
                    break
                weight = sum(s["target_words"] for s in room)
                for section in room:
                    extra = -(-deficit * section["target_words"] // weight)  # ceil
                    section["target_words"] = min(max_section_words, section["target_words"] + -(-extra // 10) * 10)
            repairs.append(f"target_words total raised {before} -> {sum(s['target_words'] for s in sections)}")
    return sections, repairs
