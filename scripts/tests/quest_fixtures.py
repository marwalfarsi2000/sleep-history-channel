"""Shared fixtures for the IMMERSIVE LOW-STAKES HISTORICAL QUEST tests (premises, outlines, gates)."""
from __future__ import annotations

SCORE_NAMES = (
    "gentle_curiosity", "low_arousal", "quest_clarity", "historical_immersion", "sensory_potential",
    "recoverability", "long_form_sustainability", "curiosity_decay_potential", "low_stakes", "variety",
)


def candidate(index: int, family: str, score: int, story_format: str = "CRAFT_QUEST") -> dict:
    """A complete, valid, low-stakes Story Premise."""
    return {
        "topic_ar": f"موضوع {index}",
        "angle_ar": f"زاوية {index}",
        "slug": f"topic-{index}",
        "search_query_en": f"historical topic {index}",
        "period_ar": "فترة تاريخية",
        "location_ar": "مدينة تاريخية",
        "family": family,
        "story_format": story_format,
        "key_elements": ["السوق", "الحرفة"],
        "reason_ar": "مناسب للنوم",
        "character_ar": "عطار اسمه يوسف",
        "protagonist_kind": "COMPOSITE_FICTIONAL",
        "small_want_ar": "إعادة تكوين رائحة قديمة",
        "quest_ar": "تجربة عدة تركيبات حتى تقترب الرائحة",
        "movement_ar": "يزور باعة المواد في أجزاء السوق",
        "immersion_opportunities": ["العطور", "التجارة"],
        "low_stakes_obstacles": ["وصف غير دقيق للرائحة"],
        "quiet_resolution_ar": "تركيبة قريبة ثم مساء هادئ في السوق",
        "one_sentence_summary_ar": "عطار يحاول إعادة تكوين رائحة قديمة أثناء يوم هادئ في السوق",
        "main_plots": 1, "secrets_or_mysteries": 0, "antagonists": 0, "named_characters": 3, "penalty_flags": [],
        "narrative_follow": "مهمة العطار الصغيرة",
        "soft_narrative_spine": "من الفجر إلى المساء",
        "gentle_curiosity": "هل تنجح هذه التركيبة؟",
        "attentional_anchor": "عطار يسير في السوق",
        "sensory_potential_notes": "الضوء والأصوات البعيدة والروائح",
        "tension_risks": [],
        "reentry_plan": "مشاهد مستقلة مترابطة",
        "long_form_capacity": "أماكن وحرف متنوعة بلا حشو",
        "difference_from_recent": "عائلة ومكان مختلفان",
        "scores": {name: score for name in SCORE_NAMES},
    }


def gate(**overrides) -> dict:
    result = {
        "status": "PASS", "language": True, "religion": True, "sleep": True, "quality": True,
        "universality": True, "non_duplicate": True, "soft_narrative_spine": True,
        "gentle_curiosity": True, "attentional_anchoring": True, "reentry_tolerance": True,
        "gradual_unwind": True, "long_form_without_filler": True, "clear_narrative_thread": True,
        "scene_openable": True, "divisible_into_scenes": True,
        "understandable_without_prior_knowledge": True, "gradually_calmable": True,
        "not_a_fact_list": True, "has_small_want": True, "low_stakes_quest": True,
        "not_mystery_or_thriller": True, "summarizable_in_one_sentence": True, "not_overplotted": True,
        "not_boring_routine": True, "verifiable_historical_world": True, "fiction_clearly_separated": True,
        "scores": {name: 5 for name in SCORE_NAMES}, "penalty_flags": [], "reasons": [],
    }
    result.update(overrides)
    return result


def quest_decorate(sections: list[dict]) -> list[dict]:
    """Add the quest fields to a plain outline: a quest that progresses, resolves early and fades out with decaying curiosity."""
    count = len(sections)
    for index, section in enumerate(sections):
        phase = section.get("phase")
        section.setdefault("quest_beat", f"محاولة {index + 1}: خطوة صغيرة في المهمة")
        if phase == "CALM_HOOK":
            state, opened = "introduced", 1
        elif index == count - 1:
            state, opened = "aftermath", 0
        elif index == count - 2:
            state, opened = "resolved", 0
        elif index == count - 3:
            state, opened = "nearly_solved", 0
        else:
            state, opened = "in_progress", 1 if index < max(1, count // 3) + 1 else 0
        if phase in ("UNWIND", "SOFT_FADE"):
            opened = 0
        if phase == "SETTLE":
            opened = min(opened, 0)
        section["quest_state"] = state
        section["new_questions_opened"] = opened
        section["dialogue_level"] = "none" if phase in ("UNWIND", "SOFT_FADE") else "light"
        section["named_characters"] = ["يوسف"]
        for j, scene in enumerate(section.get("scenes") or []):
            if isinstance(scene, dict):
                scene["place_or_anchor"] = f"مكان {index + 1}-{j + 1}"
    return sections
