"""THUMBNAIL_HOOK_WRITER: decides *what* the thumbnail says (copy only).

The typography renderer decides how it is drawn. This module asks the model for
six to eight short candidate hooks of different kinds, rejects weak ones with
deterministic rules, and picks the best by the model's per-criterion scores.
"""

from __future__ import annotations

import json
import re

HOOK_TYPES = (
    "curiosity_question", "discovery", "transformation",
    "inside_immersion", "object_process_mystery", "historical_contrast",
)
SCORE_KEYS = (
    "curiosity", "clarity", "mobile_readability", "specificity",
    "title_complement", "historical_accuracy", "elegance",
)
MIN_WORDS, MAX_WORDS, PREFERRED_MAX_WORDS = 2, 5, 4
MIN_CANDIDATES = 4
MIN_CURIOSITY, MIN_CLARITY = 3, 3
TITLE_OVERLAP_LIMIT = 0.75

# Opening words of generic scene labels ("workshops of ...", "life in ...").
GENERIC_FIRST_WORDS = {
    "ورش", "ورشة", "سوق", "اسواق", "سواقي", "ساقية", "حياة", "مدينة", "مدن",
    "شوارع", "احياء", "بيوت", "حرف", "حرفة", "صناعة", "اجواء", "مشاهد", "يوم",
}
GENERIC_LAST_WORDS = {"القديمة", "القديم", "التاريخية", "التاريخي", "قديمة", "قديم"}
FORBIDDEN_LABELS = {"قصة للنوم", "قصة تاريخية", "حكاية للنوم", "للنوم", "قبل النوم"}
CLICKBAIT = (
    "لن تصدق", "صادم", "مرعب", "ما حدث بعد", "ستندهش", "الحقيقة المخيفة", "ممنوع",
    "خطير", "فضيحة", "كارثة", "!", "؟؟",
)

_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟـ]")  # harakat, tatweel
_NON_ARABIC = re.compile(r"[^ء-ي\s]")


def normalize_words(text: str) -> list[str]:
    """Arabic words without diacritics/tatweel, folded alef, definite article stripped."""
    text = _MARKS.sub("", text)
    text = re.sub("[إأآٱ]", "ا", text).replace("ى", "ي").replace("ة", "ه")
    words = _NON_ARABIC.sub(" ", text).split()
    return [word[2:] if word.startswith("ال") and len(word) > 3 else word for word in words]


GENERIC_FIRST_WORDS = {normalize_words(word)[0] for word in GENERIC_FIRST_WORDS}
GENERIC_LAST_WORDS = {normalize_words(word)[0] for word in GENERIC_LAST_WORDS}


def word_count(text: str) -> int:
    return len(re.findall(r"[ء-ي]+", text))


def hook_rejection(text: str, title_core: str, recent_labels: list[str]) -> str:
    """Reason a hook must not be used, or '' when it is acceptable."""
    label = str(text).strip()
    words = normalize_words(label)
    count = word_count(label)
    if not MIN_WORDS <= count <= MAX_WORDS:
        return f"has {count} Arabic words; it must be {MIN_WORDS}-{MAX_WORDS}"
    if "😴" in label or re.search(r"[\U0001F300-\U0001FAFF]", label):
        return "contains an emoji"
    if label in FORBIDDEN_LABELS:
        return "is a generic label"
    if any(mark in label for mark in CLICKBAIT):
        return "is cheap clickbait"
    first = words[0] if words else ""
    last = normalize_words(label)[-1] if words else ""
    if first in GENERIC_FIRST_WORDS or last in GENERIC_LAST_WORDS:
        return "is a generic scene label, not a hook"
    title_words = set(normalize_words(title_core))
    if words and (
        words == normalize_words(title_core)
        or sum(1 for word in words if word in title_words) / len(words) >= TITLE_OVERLAP_LIMIT
    ):
        return "repeats the title instead of complementing it"
    recent = {" ".join(normalize_words(item)) for item in recent_labels}
    if " ".join(words) in recent:
        return "repeats a recent thumbnail hook"
    return ""


def _score(candidate: dict) -> float:
    scores = candidate.get("scores") or {}
    total = sum(float(scores.get(key, 0)) for key in SCORE_KEYS)
    if word_count(candidate["text"]) > PREFERRED_MAX_WORDS:
        total -= 3.0  # a fifth word must be clearly stronger to win
    return total


def choose_hook(candidates: list[dict], title_core: str, recent_labels: list[str]) -> tuple[str, list[dict]]:
    """Pick the best acceptable candidate. Returns (text, rejected-with-reasons)."""
    rejected: list[dict] = []
    viable: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        item = {**item, "text": str(item["text"]).strip()}
        reason = hook_rejection(item["text"], title_core, recent_labels)
        scores = item.get("scores") or {}
        if not reason and (
            float(scores.get("curiosity", 0)) < MIN_CURIOSITY or float(scores.get("clarity", 0)) < MIN_CLARITY
        ):
            reason = "scores too low for curiosity or clarity"
        if reason:
            rejected.append({"text": item["text"], "reason": reason})
        else:
            viable.append(item)
    if not viable:
        return "", rejected
    best = max(viable, key=lambda item: (_score(item), -word_count(item["text"])))
    return best["text"], rejected


def build_prompt(topic: dict, title_core: str, recent_labels: list[str], rejected: list[dict] | None = None) -> str:
    feedback = ""
    if rejected:
        feedback = "\nمرشحون مرفوضون سابقًا (لا تكررهم):\n" + json.dumps(rejected, ensure_ascii=False)
    return f"""أنت كاتب عبارات المصغّرة (THUMBNAIL_HOOK_WRITER) لقناة تاريخية عربية هادئة للنوم.
مهمتك كتابة *النص فقط*؛ أما الرسم فيتولاه محرك الطباعة لاحقًا.

الموضوع: {topic.get('topic_ar', '')} — {topic.get('angle_ar', '')}
عنوان الحلقة (لا تكرره): {title_core}
عبارات حديثة (تجنبها): {json.dumps(recent_labels, ensure_ascii=False)}{feedback}

ولّد من 6 إلى 8 عبارات قصيرة جدًا (من كلمتين إلى أربع كلمات، وخمس فقط إذا كانت أقوى بوضوح) من أنواع مختلفة:
{', '.join(HOOK_TYPES)}.
المطلوب: فضول حقيقي، وضوح فوري على الهاتف، تحديد خاص بهذه الحلقة، تكامل مع العنوان بلا تكرار، دقة تاريخية، أناقة.
ممنوع: تسميات المشاهد العامة مثل «ورش صناعة…» أو «أسواق … القديمة» أو «سواقي …» أو «حياة …»، وclickbait الرخيص، والإيموجي، وتكرار العنوان.

أعد JSON فقط:
{{"candidates":[{{"text":"...","type":"one of the types","scores":{{{', '.join(f'"{key}":0' for key in SCORE_KEYS)}}}}}]}}
كل درجة عدد صحيح من صفر إلى خمسة."""


def write_hook(gemini, parse_json, topic: dict, title_core: str, recent_labels: list[str], attempts: int = 2) -> str:
    """Ask for candidates and return the chosen hook ('' when none is acceptable)."""
    rejected: list[dict] = []
    for _ in range(attempts):
        text, _usage = gemini(
            build_prompt(topic, title_core, recent_labels, rejected),
            json_mode=True, max_tokens=2500, temperature=0.8,
        )
        try:
            payload = parse_json(text)
        except ValueError:
            continue
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        if not isinstance(candidates, list) or len(candidates) < MIN_CANDIDATES:
            continue
        hook, newly_rejected = choose_hook(candidates, title_core, recent_labels)
        if hook:
            return hook
        rejected.extend(newly_rejected)
    return ""
