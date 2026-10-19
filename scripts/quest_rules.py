"""IMMERSIVE LOW-STAKES HISTORICAL QUEST: the deterministic half of the channel's narrative identity.

The prompts ask the model for the quest; this module is what the code enforces without trusting the model:
premise shape, mystery/high-stakes markers, outline curiosity-decay and recoverability structure, and a few
cheap text checks. Nothing here calls a model or reads the network.

Identity: a small fictional-but-plausible composite character with one small, clear want moves through a
verified historical world. Curiosity is gentle and decays; the quest resolves early enough to leave a quiet
aftermath. The listener should want to stay in the world, not need to know the ending.
"""

from __future__ import annotations

import re

IDENTITY = "IMMERSIVE_LOW_STAKES_HISTORICAL_QUEST"
GOLDEN_RULE = "THE LISTENER SHOULD WANT TO STAY IN THE WORLD, NOT NEED TO KNOW THE ENDING."

# Immersion flavors: the quest is always a quest; the flavor only varies what kind of small task it is.
QUEST_FLAVORS = (
    "CRAFT_QUEST", "MARKET_QUEST", "JOURNEY_QUEST", "PREPARATION_QUEST", "SEARCH_FOR_MATERIAL",
    "MAKE_OR_REPAIR", "TRADE_OR_DELIVERY", "HOSTING_OR_ARRIVAL", "SEASONAL_TASK", "WORKSHOP_QUEST",
)
# Formats of the previous identity. They stay readable in history (so rotation still sees them) but are never
# valid for a new episode: "a place explained" is not a quest.
LEGACY_FORMATS = (
    "JOURNEY", "DAY_IN_THE_LIFE", "PLACE_IMMERSION", "HOW_IT_WORKED", "EVERYDAY_HABITS", "CLEAR_HISTORICAL_ARC",
)

SCORE_NAMES = (
    "gentle_curiosity", "low_arousal", "quest_clarity", "historical_immersion", "sensory_potential",
    "recoverability", "long_form_sustainability", "curiosity_decay_potential", "low_stakes", "variety",
)
MIN_SCORE = 3
CRITICAL_SCORES = {"low_arousal": 4, "low_stakes": 4, "quest_clarity": 4, "recoverability": 4}
MIN_AVERAGE = 4.0

PENALTY_FLAGS = (
    "HIGH_STAKES", "MYSTERY_DEPENDENCE", "VIOLENCE", "DANGER", "CLIFFHANGER_DEPENDENCE", "HEAVY_CONFLICT",
    "TOO_MUCH_POLITICS", "TOO_MANY_CHARACTERS", "TOO_MANY_DATES", "ENCYCLOPEDIC_STRUCTURE",
)

PROTAGONIST_KINDS = ("COMPOSITE_FICTIONAL", "DOCUMENTED_HISTORICAL")

PREMISE_FIELDS = (
    "character_ar", "protagonist_kind", "small_want_ar", "quest_ar", "movement_ar",
    "immersion_opportunities", "low_stakes_obstacles", "quiet_resolution_ar", "one_sentence_summary_ar",
)

# Premises that are mysteries, thrillers or high-stakes. Phrases, not single words, so ordinary words stay usable.
_REJECTED = (
    r"من\s+قتل", r"جريمة", r"قتل", r"لغز", r"مؤامرة", r"مطاردة", r"اختفاء|اختفى", r"مفقود", r"خيانة",
    r"انتقام", r"الصندوق\s+الغامض", r"رسالة\s+(?:سرية|غامضة)", r"سر\s+(?:خطير|قد|دفين|مظلم)",
    r"قبل\s+فوات\s+الأوان", r"سباق\s+مع\s+الزمن", r"خطر\s+على\s+حياة", r"نجاة|ينجو", r"صدمة|صادم",
    r"سيغير\s+(?:مصير|المدينة)", r"تهديد",
)
REJECTED_PREMISE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in _REJECTED))
_PREMISE_TEXT_FIELDS = (
    "topic_ar", "angle_ar", "small_want_ar", "quest_ar", "movement_ar", "quiet_resolution_ar",
    "one_sentence_summary_ar", "narrative_follow", "gentle_curiosity",
)


def normalize_flavor(value: object) -> str:
    return re.sub(r"\s+", "_", str(value or "").strip().upper())


def premise_hazards(candidate: dict) -> list[str]:
    """Mystery / thriller / high-stakes markers found in the premise text (quest-style, not word lists of history)."""
    found: list[str] = []
    for field in _PREMISE_TEXT_FIELDS:
        text = str(candidate.get(field) or "")
        match = REJECTED_PREMISE_RE.search(text)
        if match:
            found.append(f"{field}: {match.group(0)}")
    flags = {str(flag).strip().upper() for flag in (candidate.get("penalty_flags") or [])}
    found.extend(f"penalty flag: {flag}" for flag in sorted(flags & set(PENALTY_FLAGS)))
    return found


def nonempty(value: object) -> bool:
    if isinstance(value, (list, tuple)):
        return any(str(item).strip() for item in value)
    return bool(str(value or "").strip())


def premise_errors(candidate: dict) -> list[str]:
    """Structural problems of a story premise (empty when it is a complete, low-stakes, single-quest premise)."""
    errors = [f"missing premise field: {name}" for name in PREMISE_FIELDS if not nonempty(candidate.get(name))]
    if normalize_flavor(candidate.get("story_format")) not in QUEST_FLAVORS:
        errors.append(f"story_format must be one of the quest flavors, got {candidate.get('story_format')!r}")
    kind = str(candidate.get("protagonist_kind") or "").strip().upper()
    if kind and kind not in PROTAGONIST_KINDS:
        errors.append("protagonist_kind must be COMPOSITE_FICTIONAL or DOCUMENTED_HISTORICAL")
    summary = str(candidate.get("one_sentence_summary_ar") or "").strip()
    if summary and (len(re.findall(r"[؟?!.…]", summary.rstrip("."))) > 0 or len(summary.split()) > 40):
        errors.append("the quest must be summarizable in one simple sentence")
    for name in ("main_plots", "secrets_or_mysteries", "antagonists"):
        value = candidate.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            limit = 1 if name == "main_plots" else 0
            if (value != limit) if name == "main_plots" else (value > limit):
                errors.append(f"overplotting: {name}={value}")
    names = candidate.get("named_characters")
    if isinstance(names, int) and not isinstance(names, bool) and names > 6:
        errors.append(f"overplotting: {names} named characters")
    errors.extend(f"rejected premise marker ({item})" for item in premise_hazards(candidate))
    return errors


# ---------------------------------------------------------------- outline: curiosity decay, recoverability, anti-boring
QUEST_STATES = ("introduced", "in_progress", "nearly_solved", "resolved", "aftermath")
DIALOGUE_LEVELS = ("none", "light", "normal")
LATE_PHASES = ("UNWIND", "SOFT_FADE")
MAX_NAMED_CHARACTERS = 6


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def quest_outline_errors(sections: list[dict]) -> list[str]:
    """Curiosity decay curve + quest shape of an outline whose sections carry the quest fields."""
    errors: list[str] = []
    opened = [_int(section.get("new_questions_opened")) for section in sections]
    if any(value is None or not 0 <= value <= 3 for value in opened):
        errors.append("Every outline section needs new_questions_opened (an integer 0-3)")
        return errors
    states = [str(section.get("quest_state", "")).strip() for section in sections]
    if any(state not in QUEST_STATES for state in states):
        errors.append(f"Every outline section needs quest_state, one of {', '.join(QUEST_STATES)}")
        return errors
    phases = [str(section.get("phase", "")).strip() for section in sections]
    count = len(sections)
    if opened[0] < 1:
        errors.append("CALM_HOOK must open the gentle first question (new_questions_opened >= 1)")
    for phase, value, number in zip(phases, opened, range(1, count + 1)):
        if phase == "SETTLE" and value > 1:
            errors.append(f"section {number} (SETTLE) opens {value} new questions; at most 1")
        if phase in LATE_PHASES and value != 0:
            errors.append(f"section {number} ({phase}) opens a new question; the last quarter opens none")
    third = max(1, count // 3)
    early, late = sum(opened[:third]), sum(opened[-third:])
    if late >= early and early > 0:
        errors.append(f"Curiosity does not decay: the first third opens {early} questions, the last third {late}")
    if states[-1] != "aftermath":
        errors.append("The final section must be the quiet aftermath of a quest that is already resolved (quest_state=aftermath)")
    if "resolved" not in states[:-1] and "nearly_solved" not in states[:-1]:
        errors.append("The quest must progress toward a resolution before the final section")
    first_resolved = next((i for i, state in enumerate(states) if state in ("resolved", "aftermath")), count - 1)
    if first_resolved >= count - 1 and count > 1:
        errors.append("The quest is resolved only in the last section: resolve it earlier and leave a quiet aftermath")
    for number, (phase, state) in enumerate(zip(phases, states), 1):
        if phase == "SOFT_FADE" and state not in ("resolved", "aftermath"):
            errors.append(f"section {number} (SOFT_FADE) is not after the resolution")
        if phase == "CALM_HOOK" and state not in ("introduced", "in_progress"):
            errors.append(f"section {number} (CALM_HOOK) must introduce the quest")
    levels = [str(section.get("dialogue_level", "")).strip() for section in sections]
    if any(level not in DIALOGUE_LEVELS for level in levels):
        errors.append(f"Every outline section needs dialogue_level, one of {', '.join(DIALOGUE_LEVELS)}")
    elif any(level == "normal" for phase, level in zip(phases, levels) if phase in LATE_PHASES):
        errors.append("Dialogue must fade out: UNWIND and SOFT_FADE use dialogue_level none or light")
    # anti-boring: the quest has to move.
    progress = [str(section.get("quest_beat", "")).strip() for section in sections if str(section.get("quest_beat", "")).strip()]
    if len(progress) < max(3, count // 2) or len({beat for beat in progress}) < 3:
        errors.append("Underplotting: the quest needs at least three distinct quest_beat steps (attempts, stops, discoveries)")
    places = {
        str(scene.get("place_or_anchor", "")).strip()
        for section in sections for scene in section.get("scenes") or [] if isinstance(scene, dict)
    }
    if len(places) < 5:
        errors.append("Underplotting: the quest needs movement through at least five distinct places or anchors")
    named: set[str] = set()
    for section in sections:
        value = section.get("named_characters")
        if isinstance(value, list):
            named.update(str(item).strip() for item in value if str(item).strip())
    if len(named) > MAX_NAMED_CHARACTERS:
        errors.append(f"Overplotting: {len(named)} named characters; at most {MAX_NAMED_CHARACTERS}")
    for number, section in enumerate(sections, 1):
        for flag in ("twist", "cliffhanger", "countdown", "betrayal", "danger"):
            if section.get(flag) is True:
                errors.append(f"section {number} declares {flag}: not allowed in a low-stakes quest")
    return errors


# ---------------------------------------------------------------- text: cheap, conservative checks
_ENDING_BANNED = (
    "شكرا على الاستماع", "شكرًا على الاستماع", "شكراً على الاستماع", "في الحلقة القادمة", "في الحلقه القادمه",
    "لم يكن يعرف ما ينتظره", "لم تكن تعرف ما ينتظرها", "ما ينتظره غدا", "ما ينتظره غدًا", "اشترك", "لا تنس",
)
_ENCYCLOPEDIC_OPENING = re.compile(r"^\s*(?:تعتبر|تُعتبر|تُعد|تعد|تقع|يعتبر|يُعتبر)\b")
_LATE_MYSTERY = re.compile(r"لغز|مؤامرة|فجأة\s+ظهر|صدمة|تبين\s+أن\s+كل|الحقيقة\s+الصادمة")


def _words(text: str) -> list[str]:
    return re.findall(r"[؀-ۿ]+", text)


def _slice_by_words(text: str, start: float, end: float) -> str:
    tokens = text.split()
    return " ".join(tokens[int(len(tokens) * start): int(len(tokens) * end)])


def question_density(text: str) -> float:
    """Questions per 1000 words in the given text."""
    words = len(_words(text)) or 1
    return 1000 * len(re.findall(r"[؟?]", text)) / words


def story_text_errors(text: str) -> list[str]:
    """Deterministic, conservative checks of a finished story (the model gate decides the rest)."""
    errors: list[str] = []
    if _ENCYCLOPEDIC_OPENING.match(text.lstrip("# \n")):
        errors.append("encyclopedic opening: start with a scene (a character doing something), not with a definition")
    tail = text[-1500:]
    if any(phrase in tail for phrase in _ENDING_BANNED):
        errors.append("the ending contains a cliffhanger, teaser, call to action or thanks")
    if _LATE_MYSTERY.search(_slice_by_words(text, 0.9, 1.0)):
        errors.append("a mystery, twist or shock appears in the final 10%")
    early = question_density(_slice_by_words(text, 0.0, 0.5))
    final = question_density(_slice_by_words(text, 0.8, 1.0))
    if final > max(1.5, 0.6 * early):
        errors.append(f"curiosity does not decay: {final:.1f} questions per 1000 words in the last 20% vs {early:.1f} in the first half")
    return errors
