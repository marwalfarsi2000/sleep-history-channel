"""RESEARCH_TOPIC_ALIGNMENT_GATE: the saved research must support the episode's narrative spine.

Topic selection can pick an attractive angle (a craft, a product, a family
routine) that the research then fails to document. This gate runs after
research and before the outline: it keeps the episode, re-anchors the angle on
what the sources actually support, and stops safely (resumable) when even that
is not possible. It never invents details to finish a story.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PASS, REANCHOR_REQUIRED, RESEARCH_INSUFFICIENT = "PASS", "REANCHOR_REQUIRED", "RESEARCH_INSUFFICIENT"
STATUSES = (PASS, REANCHOR_REQUIRED, RESEARCH_INSUFFICIENT)
CHECKED_FIELDS = (
    "topic_ar", "angle_ar", "narrative_follow", "soft_narrative_spine", "gentle_curiosity", "key_elements",
)
REANCHORED_FIELDS = ("angle_ar", "narrative_follow", "soft_narrative_spine", "gentle_curiosity", "key_elements")
RESEARCH_LIMIT = 45000
METADATA_KEYS = (
    "research_topic_alignment_status", "unsupported_topic_elements", "reanchored_elements", "alignment_reason",
)


class AlignmentError(RuntimeError):
    """Raised with a registered error code prefix; the episode stays checkpointed."""


def _plain(value: object) -> object:
    return value if isinstance(value, (str, int, float, bool, list, dict)) else str(value)


def topic_view(topic: dict) -> dict:
    return {field: _plain(topic.get(field)) for field in CHECKED_FIELDS if topic.get(field)}


def input_hash(topic: dict, research: str) -> str:
    payload = json.dumps(topic_view(topic), ensure_ascii=False, sort_keys=True) + "\n" + research
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_check_prompt(topic: dict, research: str) -> str:
    return f"""أنت بوابة مواءمة بين موضوع حلقة تاريخية ونتائج البحث المحفوظ لها.
مهمتك: هل تدعم المصادر وخريطة الادعاءات فعلًا العناصر التي ستُبنى عليها الحلقة؟ لا تحكم من معرفتك العامة؛ احكم فقط مما ورد في البحث.

عناصر الحلقة:
{json.dumps(topic_view(topic), ensure_ascii=False, indent=2)}

البحث (يتضمن خريطة الادعاءات والمصادر):
{research[:RESEARCH_LIMIT]}

أعد JSON فقط:
{{"status":"PASS أو REANCHOR_REQUIRED أو RESEARCH_INSUFFICIENT","supported_elements":["عناصر مدعومة بالمصادر"],"unsupported_elements":["عناصر في الموضوع/الزاوية/العمود السردي لا تثبتها المصادر"],"reason":"سبب موجز"}}
PASS: كل العناصر الأساسية (العمود السردي وما نتبعه والفضول اللطيف والعناصر الرئيسية) مدعومة.
REANCHOR_REQUIRED: المكان والفترة مدعومان لكن بعض عناصر الزاوية غير موثقة، ويوجد في البحث ما يكفي لبناء زاوية هادئة بديلة.
RESEARCH_INSUFFICIENT: البحث لا يكفي حتى لزاوية بديلة موثقة."""


def build_reanchor_prompt(topic: dict, research: str, unsupported: list, supported: list) -> str:
    return f"""أعد بناء زاوية الحلقة نفسها اعتمادًا فقط على ما تدعمه المصادر في البحث أدناه. لا تغيّر المكان ولا الفترة ولا تبدأ موضوعًا جديدًا، ولا تضف أي تفصيل غير وارد في البحث.

الموضوع: {topic.get('topic_ar', '')}
الفترة: {topic.get('period_ar', '')} — المكان: {topic.get('location_ar', '')}
العناصر غير المدعومة (أزلها كليًا): {json.dumps(unsupported, ensure_ascii=False)}
عناصر مدعومة: {json.dumps(supported, ensure_ascii=False)}
الصيغة المطلوبة للحلقة: {topic.get('story_format', '')} — هادئة للنوم، بلا شخصيات مخترعة.

البحث:
{research[:RESEARCH_LIMIT]}

أعد JSON فقط:
{{"angle_ar":"...","narrative_follow":"ما الذي سيتبعه المستمع","soft_narrative_spine":"...","gentle_curiosity":"سؤال لطيف يجيب عنه البحث","key_elements":["..."]}}"""


def _as_list(value: object) -> list:
    return [str(item) for item in value] if isinstance(value, list) else ([str(value)] if value else [])


def check(gemini, parse_json, topic: dict, research: str) -> dict:
    for _ in range(2):
        text, _usage = gemini(build_check_prompt(topic, research), json_mode=True, max_tokens=2500, temperature=0.0)
        try:
            payload = parse_json(text)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("status") not in STATUSES:
            continue
        result = {
            "status": payload["status"],
            "supported_elements": _as_list(payload.get("supported_elements")),
            "unsupported_elements": _as_list(payload.get("unsupported_elements")),
            "reason": str(payload.get("reason") or "")[:500],
        }
        if result["status"] == PASS and result["unsupported_elements"]:
            result["status"] = REANCHOR_REQUIRED  # a PASS that lists unsupported elements contradicts itself
        return result
    raise AlignmentError("RESEARCH_TOPIC_ALIGNMENT_FAILED: the alignment check returned no usable verdict")


def reanchor(gemini, parse_json, topic: dict, research: str, result: dict) -> dict:
    for _ in range(2):
        text, _usage = gemini(
            build_reanchor_prompt(topic, research, result["unsupported_elements"], result["supported_elements"]),
            json_mode=True, max_tokens=2500, temperature=0.2)
        try:
            payload = parse_json(text)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        fields = {
            "angle_ar": str(payload.get("angle_ar") or "").strip(),
            "narrative_follow": str(payload.get("narrative_follow") or "").strip(),
            "soft_narrative_spine": str(payload.get("soft_narrative_spine") or "").strip(),
            "gentle_curiosity": str(payload.get("gentle_curiosity") or "").strip(),
            "key_elements": [item for item in _as_list(payload.get("key_elements")) if item.strip()],
        }
        if all(fields.values()):
            return fields
    raise AlignmentError("RESEARCH_TOPIC_ALIGNMENT_FAILED: could not re-anchor the angle on the supported elements")


def _record(topic: dict, status: str, unsupported: list, reanchored: list, reason: str) -> dict:
    return {
        "research_topic_alignment_status": status,
        "unsupported_topic_elements": unsupported,
        "reanchored_elements": reanchored,
        "alignment_reason": reason,
    }


def run_gate(
    gemini, parse_json, topic: dict, research: str, *, cache_path: Path | None = None, search_more=None,
) -> tuple[dict, str, dict]:
    """Return ``(topic, research, record)``; raise ``AlignmentError`` when no supported angle exists.

    ``search_more(topic) -> str`` may return extra, validated research text; it is used at most once.
    """
    topic = dict(topic)
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if cached.get("input_hash") == input_hash(topic, research) and cached.get("record", {}).get(
                "research_topic_alignment_status") == PASS:
            return topic, research, cached["record"]

    first = check(gemini, parse_json, topic, research)
    unsupported, reanchored, reason, extra_used = list(first["unsupported_elements"]), [], first["reason"], False
    result = first
    if result["status"] != PASS and search_more is not None and result["status"] == RESEARCH_INSUFFICIENT:
        research = f"{research.rstrip()}\n\n## بحث إضافي محدود\n{search_more(topic)}\n"
        extra_used = True
        result = check(gemini, parse_json, topic, research)
    if result["status"] == REANCHOR_REQUIRED:
        fields = reanchor(gemini, parse_json, topic, research, result)
        old_elements = set(_as_list(topic.get("key_elements")))
        reanchored = [item for item in fields["key_elements"] if item not in old_elements] or fields["key_elements"]
        topic.update(fields)
        result = check(gemini, parse_json, topic, research)
        if result["status"] != PASS and search_more is not None and not extra_used:
            research = f"{research.rstrip()}\n\n## بحث إضافي محدود\n{search_more(topic)}\n"
            result = check(gemini, parse_json, topic, research)
    if result["status"] != PASS:
        raise AlignmentError(
            "RESEARCH_TOPIC_ALIGNMENT_INSUFFICIENT: the saved sources do not support a documented angle "
            f"for this episode ({result['reason'] or 'no reason given'})"[:400])
    record = _record(topic, PASS, unsupported, reanchored,
                     reason if not reanchored else f"{reason} Angle re-anchored on supported elements."[:500])
    topic.update(record)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(
            {"input_hash": input_hash(topic, research), "record": record}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return topic, research, record
