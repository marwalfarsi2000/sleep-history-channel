from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import episode_selection
import llm_router
import outline_repair
import quest_rules
import research_alignment
import run_summary
import state_store


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
MASTER_RULES_PATH = ROOT / "config" / "master_production_rules.md"
TOPIC_CANDIDATE_BANK_PATH = ROOT / "config" / "topic_candidate_bank.json"
ARCHIVE_INDEX_PATH = ROOT / "config" / "archive_index.json"
YOUTUBE_PACKAGING_RULES_PATH = ROOT / "config" / "youtube_packaging_rules.md"
TITLE_SUFFIXES = (
    "قصة تاريخية للنوم 😴",
    "قصة تاريخية هادئة للنوم 😴",
    "ستنام قبل النهاية 😴",
    "حكاية تاريخية للنوم 😴",
    "رحلة هادئة إلى الماضي 😴",
    "حكاية هادئة من الماضي 😴",
    "قصة طويلة وهادئة للنوم 😴",
    "رحلة تاريخية هادئة للنوم 😴",
)
SLEEP_SCORE_NAMES = quest_rules.SCORE_NAMES  # gentle_curiosity, low_arousal, quest_clarity, historical_immersion, ...
PREFERRED_CANDIDATES = 6
MIN_VALID_CANDIDATES = 4
MIN_NARRATIVE_FAMILIES = 4
SECRETS_FILE = Path(os.environ.get(
    "SLEEP_HISTORY_SECRETS_FILE",
    SETTINGS.get(
        "secrets_file",
        str(Path.home() / "AppData/Local/SleepHistoryAutomation/secrets.env"),
    ),
))
MODEL = SETTINGS.get("llm_model", "gemini-flash-latest")
GEMINI_RETRY_DELAYS = (5, 15, 30)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_secrets() -> dict[str, str]:
    values = dict(os.environ)
    if SECRETS_FILE.exists():
        for raw in SECRETS_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not values.get(key):
                values[key] = value.strip()
    return values


def _daily_quota_exhausted(body: str) -> bool:
    """Return True when retrying before the daily reset cannot succeed."""
    normalized = body.lower().replace("_", "").replace("-", "")
    markers = (
        "quotaexceeded",
        "perday",
        "requestsperday",
        "dailyquota",
    )
    return any(marker in normalized for marker in markers)


def thinking_config_for_model(model: str) -> dict:
    """Return appropriate thinking config for a Gemini model."""
    if model == "gemini-3.8-flash":
        return {"thinkingLevel": "low"}
    if model.startswith("gemini-3") and model != "gemini-3.1-flash-lite":
        return {"thinkingLevel": "minimal"}
    return {"thinkingBudget": 0}


def _llm_models(*, search: bool = False) -> list[str]:
    configured = SETTINGS.get("llm_fallback_models", [])
    research = SETTINGS.get("research_fallback_models", []) if search else []
    if not isinstance(configured, list) or not isinstance(research, list):
        raise RuntimeError("llm_fallback_models and research_fallback_models must be JSON lists")
    # Research-only fallbacks are appended last: grounding quota is shared across
    # the Gemini 3 family, so older search-capable models extend coverage.
    models = list(dict.fromkeys([MODEL, *(str(model).strip() for model in [*configured, *research])]))
    if SETTINGS.get("cost_policy") != "free_or_preapproved_only":
        raise RuntimeError("BLOCKED_COST: unsupported LLM cost policy")
    approved = set(SETTINGS.get("approved_llm_models") or [])
    unapproved = [model for model in models if model not in approved]
    if unapproved:
        raise RuntimeError(f"BLOCKED_COST: unapproved LLM models: {unapproved}")
    return models


def _error_summary(body: str) -> str:
    """Short quota/error reason for logs, without request data."""
    try:
        error = json.loads(body).get("error", {})
    except (json.JSONDecodeError, AttributeError):
        return " ".join(body[:200].split())
    quota = [
        violation.get("quotaId") or violation.get("quotaMetric", "")
        for detail in error.get("details", []) or []
        for violation in detail.get("violations", []) or []
    ]
    return f"{error.get('status', '')} {', '.join(filter(None, quota))} {str(error.get('message', ''))[:160]}".strip()


def _gemini_one(model: str, prompt: str, *, json_mode: bool = False, search: bool = False,
                max_tokens: int = 8192, temperature: float = 0.4) -> tuple[str, list[dict]]:
    key = load_secrets().get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError(f"GEMINI_API_KEY is missing from {SECRETS_FILE}")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={key}"
    )
    config: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        "thinkingConfig": thinking_config_for_model(model),
    }
    if json_mode:
        config["responseMimeType"] = "application/json"
    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": config,
    }
    if search:
        payload["tools"] = [{"google_search": {}}]
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    from error_classifier import classify_error, ErrorCategory
    result = None
    last_error = ""
    retry_delays = GEMINI_RETRY_DELAYS
    unsupported_thinking_recovered = False
    for attempt in range(len(retry_delays) + 1):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            last_error = f"Gemini HTTP {error.code}: {body[:600]}"
            if (
                error.code == 400
                and not unsupported_thinking_recovered
                and "thinking level" in body.lower()
                and "not supported" in body.lower()
            ):
                unsupported_thinking_recovered = True
                config["thinkingConfig"] = {"thinkingLevel": "low"}
                payload["generationConfig"] = config
                request = urllib.request.Request(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                print(
                    "Gemini HTTP 400: unsupported thinking level; retrying with 'low'",
                    file=sys.stderr,
                    flush=True,
                )
                with urllib.request.urlopen(request, timeout=180) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            info = llm_router.classify_gemini_error(error.code, body, error.headers)
            # Rate limits (429) are never retried against the same model here: the
            # router records the cooldown and moves on or waits. Only transient
            # server-side failures get short in-place retries.
            if info.kind != llm_router.QuotaKind.CAPACITY or attempt == len(retry_delays):
                prefix = "GROUNDED_RESEARCH_UNAVAILABLE: " if search else ""
                raise llm_router.GeminiApiError(prefix + last_error, info) from error
            delay = retry_delays[attempt]
            if info.retry_after:
                delay = max(delay, int(info.retry_after))
            print(
                f"Gemini temporary HTTP {error.code}; retrying in {delay}s "
                f"({attempt + 1}/{len(retry_delays)}): {_error_summary(body)}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            # Transport failures are temporary provider capacity/network problems.
            # Keep them inside the model router so it can retry briefly and then
            # fall back to the next approved free model instead of aborting the
            # whole production run during long tasks such as diacritization.
            last_error = f"Gemini transient network error: {type(error).__name__}: {_error_summary(str(error))}"
            if attempt < len(retry_delays):
                delay = retry_delays[attempt]
                print(
                    f"Gemini transient network failure; retrying in {delay}s "
                    f"({attempt + 1}/{len(retry_delays)}): {_error_summary(str(error))}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                continue
            info = llm_router.ApiErrorInfo(
                0,
                llm_router.QuotaKind.CAPACITY,
                "model",
                None,
                [],
                last_error[:180],
            )
            raise llm_router.GeminiApiError(last_error, info) from error
    if result is None:
        raise RuntimeError(last_error or "Gemini request failed")
    candidates = result.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidate")
    text = "".join(
        part.get("text", "")
        for part in candidates[0].get("content", {}).get("parts", [])
        if isinstance(part, dict)
    ).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    sources: list[dict] = []
    metadata = candidates[0].get("groundingMetadata", {})
    for chunk in metadata.get("groundingChunks", []) or []:
        web = chunk.get("web") or {}
        uri = web.get("uri")
        if uri and not any(item["url"] == uri for item in sources):
            sources.append({"title": web.get("title") or uri, "url": uri})
    if search and not sources:
        raise RuntimeError(
            "GROUNDED_RESEARCH_UNAVAILABLE: Gemini returned no verifiable grounding sources"
        )
    return text, sources


def gemini(prompt: str, *, json_mode: bool = False, search: bool = False,
           max_tokens: int = 8192, temperature: float = 0.4) -> tuple[str, list[dict]]:
    """Call the approved Gemini models through the quota-aware router."""
    models = _llm_models(search=search)

    def attempt(model: str) -> tuple[str, list[dict]]:
        run_summary.count("gemini")
        run_summary.add_unique("models_attempted", model)
        return _gemini_one(
            model, prompt, json_mode=json_mode, search=search,
            max_tokens=max_tokens, temperature=temperature,
        )

    try:
        return llm_router.route(models, attempt, allow_wait=not search)
    except llm_router.LlmQuotaWaiting as waiting:
        if search:
            # Grounding is optional: fall back to the search providers at once.
            raise RuntimeError("GROUNDED_RESEARCH_UNAVAILABLE: " + str(waiting)) from waiting
        raise


def parse_json(text: str) -> object:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as error:
        if error.msg != "Extra data":
            raise
        value, end = json.JSONDecoder().raw_decode(cleaned)
        trailing = cleaned[end:].strip()
        # A prose note after one complete JSON value is harmless model chatter.
        # A second structured value is ambiguous and must remain fail-closed.
        trailing_without_fence = re.sub(r"^```(?:json)?\s*", "", trailing, flags=re.I)
        if trailing_without_fence.startswith(("{", "[")):
            raise
        return value


def parse_json_object(text: str, error_code: str) -> dict:
    """Parse one model JSON object, accepting the common singleton-list wrapper only."""
    value = parse_json(text)
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    shape = f"{type(value).__name__}[{len(value)}]" if isinstance(value, list) else type(value).__name__
    raise ValueError(f"{error_code}: expected one JSON object, got {shape}")


def parse_topic_discovery_with_one_repair(text: str) -> dict:
    """Parse topic discovery JSON; if syntax is broken, repair that JSON once without regenerating ideas."""
    error_code = "TOPIC_DISCOVERY_SCHEMA_FAILED"
    try:
        value = parse_json_object(text, error_code)
    except (json.JSONDecodeError, ValueError) as first_error:
        repair_prompt = f"""أصلح صياغة JSON التالية فقط.
لا تعِد كتابة الأفكار، لا تغيّر أي قيمة أو نص أو ترتيب مرشح، ولا تضف أو تحذف مرشحًا.
المطلوب فقط تصحيح أخطاء JSON مثل علامات الاقتباس أو الفواصل أو الأقواس.
يجب أن يكون الناتج JSON object واحدًا فقط وله المفتاح العلوي "candidates".
لا تضف شرحًا أو Markdown.

JSON المكسور:
{text}
"""
        repaired, _ = gemini(repair_prompt, json_mode=True, max_tokens=7000, temperature=0.0)
        try:
            value = parse_json_object(repaired, error_code)
        except (json.JSONDecodeError, ValueError) as repair_error:
            raise RuntimeError(
                f"{error_code}: malformed JSON remained invalid after one local repair"
            ) from repair_error
        if "candidates" not in value:
            raise RuntimeError(f"{error_code}: repaired JSON is missing candidates")
        print(
            f"{error_code}: repaired malformed JSON locally once ({type(first_error).__name__})",
            file=sys.stderr,
            flush=True,
        )
    if "candidates" not in value:
        raise RuntimeError(f"{error_code}: JSON object is missing candidates")
    return value


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:70] or f"history-{dt.date.today():%Y%m%d}"


def word_count(text: str) -> int:
    return len(re.findall(r"[\u0600-\u06FF]+", text))


# Immersion flavors of the one story shape every episode has: a small low-stakes quest (see quest_rules).
# Old format names stay readable in the history for rotation but are not valid for a new episode.
STORY_FORMATS = quest_rules.QUEST_FLAVORS
STORY_PHASES = ("CALM_HOOK", "EXPLORATION", "SETTLE", "UNWIND", "SOFT_FADE")  # same names, quest meaning (rules 64-65)
MIN_MACRO_SECTIONS, MAX_MACRO_SECTIONS = 7, 10
MAX_SAME_FORMAT_IN_A_ROW = 2
# Simple recovery contract: one initial model call + at most one local correction.
# Validation failures never trigger a second whole-pipeline run inside the same job.
OUTLINE_MAX_MODEL_CALLS = 2
OUTLINE_RETRY_MAX = OUTLINE_MAX_MODEL_CALLS
MIN_OUTLINE_TARGET_WORDS = 5500
CHAPTER_EXPANSION_ATTEMPTS = 1
STORY_QUALITY_REPAIR_ATTEMPTS = 1
MIN_STORY_WORDS = 5000
STORY_TOPUP_TARGET_WORDS = 5300
STORY_TOPUP_MAX_ATTEMPTS = 8
STORY_TOPUP_MAX_REQUEST_WORDS = 1200


def chapter_preferred_words(target_words: int) -> int:
    """Soft chapter target used only to decide whether one append-only continuation is worth trying."""
    return max(550, math.ceil(target_words * 0.70))


def chapter_minimum_words(target_words: int) -> int:
    """Backward-compatible alias for the soft chapter target; chapter length is no longer a hard gate."""
    return chapter_preferred_words(target_words)


def recent_story_formats(history: list[dict], limit: int = 2) -> list[str]:
    formats = [str(item.get("story_format", "")).strip() for item in history]
    return [value for value in formats if value][-limit:]


def story_format_allowed(candidate_format: str, history: list[dict]) -> bool:
    """Reject a format that already ran twice in a row (never three in a row)."""
    recent = recent_story_formats(history, MAX_SAME_FORMAT_IN_A_ROW)
    return not (
        len(recent) == MAX_SAME_FORMAT_IN_A_ROW
        and all(value == candidate_format for value in recent)
    )


def pick_topic_by_format(eligible: list[dict], history: list[dict], *, clear_margin: int = 3) -> dict:
    """Highest sleep score wins, but variety breaks near-ties.

    A format used twice in a row is excluded; a format absent from the last two
    episodes is preferred when its score is within ``clear_margin`` of the best.
    Quality wins whenever another option is clearly better.
    """
    def score(item: dict) -> int:
        return sum(item["scores"][name] for name in SLEEP_SCORE_NAMES)

    allowed = [item for item in eligible if story_format_allowed(str(item.get("story_format", "")), history)]
    pool = allowed or eligible
    best = max(pool, key=score)
    recent = recent_story_formats(history)
    if str(best.get("story_format", "")) in recent:
        fresh = [item for item in pool if str(item.get("story_format", "")) not in recent]
        if fresh:
            alternative = max(fresh, key=score)
            if score(best) - score(alternative) < clear_margin:
                return alternative
    return best


class OutlineValidationError(RuntimeError):
    """The outline parsed but violates the required schema."""


def collect_outline_errors(sections: object) -> list[str]:
    """Every schema violation at once, in a stable order (count first)."""
    errors: list[str] = []
    if not isinstance(sections, list) or not MIN_MACRO_SECTIONS <= len(sections) <= MAX_MACRO_SECTIONS:
        errors.append(f"Outline must contain {MIN_MACRO_SECTIONS}-{MAX_MACRO_SECTIONS} macro sections")
    if not isinstance(sections, list) or not sections:
        return errors
    if not all(isinstance(section, dict) for section in sections):
        errors.append("Every outline section must be an object")
        return errors
    phases = [str(section.get("phase", "")).strip() for section in sections]
    if phases[0] != "CALM_HOOK" or phases[-1] != "SOFT_FADE":
        errors.append("Outline must start with CALM_HOOK and end with SOFT_FADE")
    if any(phase not in STORY_PHASES for phase in phases):
        errors.append("Outline contains an unknown phase")
    elif [STORY_PHASES.index(phase) for phase in phases] != sorted(STORY_PHASES.index(phase) for phase in phases) \
            or set(phases) != set(STORY_PHASES):
        errors.append("Outline must cover all five phases in order")
    bad_counts: list[str] = []
    bad_scenes: list[str] = []
    for number, section in enumerate(sections, 1):
        scenes = section.get("scenes")
        if not isinstance(scenes, list) or not 2 <= len(scenes) <= 4:
            bad_counts.append(f"{number} (has {len(scenes) if isinstance(scenes, list) else 'none'})")
            continue
        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                errors.append("Every scene must be an object")
                continue
            missing = [k for k in ("place_or_anchor", "main_idea", "transition") if not str(scene.get(k, "")).strip()]
            if missing:
                bad_scenes.append(f"section {number} scene {scene_number}: {', '.join(missing)}")
    if bad_counts:
        errors.append(f"Every outline section needs 2-4 scenes [sections {', '.join(bad_counts)}]")
    if bad_scenes:
        errors.append(f"Every scene needs place_or_anchor, main_idea and transition [{'; '.join(bad_scenes)}]")
    targets = [section.get("target_words") for section in sections]
    if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in targets):
        errors.append("Every outline section needs a positive integer target_words")
    elif sum(targets) < MIN_OUTLINE_TARGET_WORDS:
        errors.append(f"Outline target_words total is {sum(targets)}; minimum is {MIN_OUTLINE_TARGET_WORDS}")
    if not errors:
        # Curiosity decay, a resolved quest with a quiet aftermath, movement, no overplotting (rule 65).
        errors.extend(quest_rules.quest_outline_errors(sections))
    return errors


def validate_outline_sections(sections: list[dict]) -> list[dict]:
    errors = collect_outline_errors(sections)
    if errors:
        raise OutlineValidationError("; ".join(errors))
    return sections


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def production_rules(*section_numbers: int) -> str:
    """Return only the master-rule sections relevant to one model request."""
    text = MASTER_RULES_PATH.read_text(encoding="utf-8")
    wanted = set(section_numbers)
    parts: list[str] = []
    matches = list(re.finditer(r"(?m)^# (\d+)\. .+$", text))
    for index, match in enumerate(matches):
        if int(match.group(1)) not in wanted:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        parts.append(text[match.start():end].strip().rstrip("-").rstrip())
    missing = wanted - {
        int(match.group(1)) for match in matches if int(match.group(1)) in wanted
    }
    if missing:
        raise RuntimeError(f"Missing master production rule sections: {sorted(missing)}")
    return "\n\n---\n\n".join(parts)


def upsert_history_checkpoint(history_path: Path, story: Path, topic: dict, status: str) -> None:
    from state_machine import transition_state, normalize_state
    history = json.loads(history_path.read_text(encoding="utf-8"))
    record = next((item for item in history if item.get("slug") == topic["slug"]), None)
    current_status = record.get("status") if record else ""
    canonical_target = transition_state(current_status, status) if current_status else normalize_state(status)

    if record is None:
        record = {
            "slug": topic["slug"],
            "topic": topic["topic_ar"],
            "angle": topic["angle_ar"],
            "story_path": story.relative_to(ROOT).as_posix(),
            "created_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=4))).isoformat(),
        }
        history.append(record)
    record["status"] = canonical_target
    record["story_path"] = story.relative_to(ROOT).as_posix()
    for stale in ("waiting_for", "waiting_stage", "blocked_until", "quota_kind", "waiting_since"):
        record.pop(stale, None)
    # Keep enough provider input to rebuild the same episode when a hosted
    # runner disappears after persisting only the catalogue checkpoint.
    for key in (
        "topic_ar", "angle_ar", "search_query_en", "period_ar", "location_ar",
        "family", "story_format", "key_elements", "reason_ar", "narrative_follow",
        "soft_narrative_spine", "gentle_curiosity", "attentional_anchor",
        "sensory_potential_notes", "tension_risks", "reentry_plan",
        "long_form_capacity", "difference_from_recent", "scores", "selection_gate",
        *quest_rules.PREMISE_FIELDS, "penalty_flags", "narrative_identity",
        *research_alignment.METADATA_KEYS,
    ):
        if topic.get(key):
            record[key] = topic[key]
    write(history_path, json.dumps(history, ensure_ascii=False, indent=2))


def topic_from_checkpoint(record: dict) -> dict:
    """Restore a complete topic payload from old or new catalogue records."""
    topic = dict(record)
    topic["topic_ar"] = record.get("topic_ar") or record.get("topic") or record["slug"]
    topic["angle_ar"] = record.get("angle_ar") or record.get("angle") or topic["topic_ar"]
    topic["period_ar"] = record.get("period_ar") or "الفترة التاريخية المذكورة في الموضوع"
    topic["location_ar"] = record.get("location_ar") or "المكان المذكور في الموضوع"
    topic["key_elements"] = record.get("key_elements") or []
    topic["search_query_en"] = record.get("search_query_en") or record["slug"].replace("-", " ")
    return topic


def load_topic_candidate_bank() -> list[dict]:
    if not TOPIC_CANDIDATE_BANK_PATH.exists():
        return []
    value = json.loads(TOPIC_CANDIDATE_BANK_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise RuntimeError("TOPIC_CANDIDATE_BANK_INVALID: expected a JSON list")
    return value


def save_topic_candidate_bank(
    candidates: list[dict], *, selected_slug: str = "", rejected_slug: str = ""
) -> None:
    """Persist strong unused ideas and their full sleep-story design."""
    records = load_topic_candidate_bank()
    by_slug = {str(item.get("slug", "")): item for item in records if item.get("slug")}
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=4))).isoformat()
    for source in candidates:
        slug = safe_slug(str(source.get("slug", "")))
        scores = source.get("scores") or {}
        if not slug or not all(isinstance(scores.get(name), int) for name in SLEEP_SCORE_NAMES):
            continue
        average = sum(scores[name] for name in SLEEP_SCORE_NAMES) / len(SLEEP_SCORE_NAMES)
        if min(scores[name] for name in SLEEP_SCORE_NAMES) < 3 or average < 3.75:
            continue
        existing = by_slug.get(slug, {})
        status = existing.get("bank_status", "queued")
        if status not in {"selected", "rejected"}:
            status = "queued"
        record = {**existing, **source}
        record.update(
            slug=slug,
            bank_status=status,
            sleep_score_average=round(average, 3),
            first_discovered_at=existing.get("first_discovered_at", now),
            last_considered_at=now,
        )
        by_slug[slug] = record
    if selected_slug and selected_slug in by_slug:
        by_slug[selected_slug]["bank_status"] = "selected"
        by_slug[selected_slug]["selected_at"] = now
    if rejected_slug and rejected_slug in by_slug:
        by_slug[rejected_slug]["bank_status"] = "rejected"
        by_slug[rejected_slug]["rejected_at"] = now
    ordered = sorted(
        by_slug.values(),
        key=lambda item: (
            item.get("bank_status") != "queued",
            -float(item.get("sleep_score_average", 0)),
            str(item.get("first_discovered_at", "")),
        ),
    )
    write(TOPIC_CANDIDATE_BANK_PATH, json.dumps(ordered, ensure_ascii=False, indent=2))


def is_valid_topic_candidate(item: dict) -> bool:
    """A usable Story Premise: complete quest fields, a quest flavor, valid scores and no mystery/high-stakes marker."""
    scores = item.get("scores") or {}
    if not str(item.get("slug", "")).strip():
        return False
    if not all(isinstance(scores.get(name), int) and 0 <= scores[name] <= 5 for name in SLEEP_SCORE_NAMES):
        return False
    item["story_format"] = quest_rules.normalize_flavor(item.get("story_format"))
    if str(item.get("protagonist_kind") or "").strip():
        item["protagonist_kind"] = str(item["protagonist_kind"]).strip().upper()
    return not quest_rules.premise_errors(item)


def choose_topic(history: list[dict], retry_feedback: str = "") -> dict:
    prior = "\n".join(
        f"- {item.get('topic', item.get('slug', ''))} | {item.get('angle', '')} | "
        f"{item.get('period_ar', '')} | {item.get('location_ar', '')} | {item.get('key_elements', [])}"
        for item in history
    ) or "لا يوجد سجل سابق"
    bank = load_topic_candidate_bank()
    used_slugs = {str(item.get("slug", "")) for item in history}
    saved_ideas = [
        item for item in bank
        if item.get("bank_status") == "queued" and item.get("slug") not in used_slugs
    ]
    saved_summary = json.dumps(saved_ideas[:20], ensure_ascii=False)
    rules = production_rules(*range(2, 17), 62, 64, 65)
    prompt = f"""أنت محرر قناة عربية اسمها «لن تُكمل هذه القصة» تقدم **حكايات تاريخية غامرة ذات مهمة لطيفة منخفضة المخاطر** (IMMERSIVE LOW-STAKES HISTORICAL QUEST) تساعد على النوم.
التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

قبل البحث التاريخي العميق، أنشئ من ستة إلى ثمانية مرشحين، كل مرشح **Story Premise كامل** (ليس عنوان موضوع ولا «الحياة اليومية في X» ولا «كيف كان X يعمل»؛ هذه مواد داخل الحكاية وليست الحبكة)، من أربع عائلات سردية مختلفة على الأقل. لكل premise: SETTING وCHARACTER (شخصية خيالية مركّبة معقولة تاريخيًا تمثل دورًا موثقًا، أو شخصية تاريخية حقيقية دون اختراع حوارات أو أحداث خاصة لها) وSMALL WANT وQUEST (مهمة صغيرة لطيفة منخفضة المخاطر، لا لغز ولا إثارة ولا خطر ولا مؤامرة ولا شيء يجب حله قبل فوات الأوان) وWHY IT REQUIRES MOVEMENT وHISTORICAL IMMERSION OPPORTUNITIES وLOW-STAKES OBSTACLES وPOSSIBLE QUIET RESOLUTION. يجب أن تُلخَّص كل حكاية بجملة بسيطة واحدة. قيّمهم ثم اختر الأفضل. يجب أن تتوفر للفائز مصادر متحفية أو جامعية أو أثرية كثيرة عن **العالم** الذي تتحرك فيه الشخصية، وأن يحمل قصة طويلة (5000+ كلمة) بلا حشو، وألا يكرر موضوعًا أو زاوية في السجل. القاعدة الذهبية: المستمع يريد أن يبقى داخل هذا العالم، وليس مضطرًا إلى معرفة النهاية.

السجل السابق:
{prior}

بنك الأفكار القوية غير المستخدمة؛ لا تكررها بصياغة أخرى لأن النظام سيعيد إدخالها في المنافسة تلقائيًا:
{saved_summary}

{f'''تصحيح إلزامي من المحاولة السابقة:
{retry_feedback}
أنشئ دفعة مصححة تعالج هذا النقص تحديدًا. إذا كان النقص في العائلات السردية، استخدم أربع قيم family مختلفة على الأقل بوضوح، مع الحفاظ على صلاحية كل Story Premise.'''.strip() if retry_feedback else ''}

أعد JSON فقط:
{{"candidates":[{{
"topic_ar":"...","angle_ar":"...","slug":"lowercase-hyphenated","search_query_en":"... (للبحث عن العالم التاريخي لا عن الحبكة)","period_ar":"...","location_ar":"...","family":"...","story_format":"واحد من: {' | '.join(STORY_FORMATS)}","key_elements":["عناصر العالم التاريخي التي يجب أن تدعمها المصادر"],"reason_ar":"...",
"character_ar":"الاسم والدور","protagonist_kind":"COMPOSITE_FICTIONAL أو DOCUMENTED_HISTORICAL","small_want_ar":"...","quest_ar":"...","movement_ar":"لماذا تتحرك الشخصية عبر العالم","immersion_opportunities":["..."],"low_stakes_obstacles":["..."],"quiet_resolution_ar":"...","one_sentence_summary_ar":"جملة واحدة بسيطة بلا علامات ترقيم داخلية",
"main_plots":1,"secrets_or_mysteries":0,"antagonists":0,"named_characters":3,"penalty_flags":[],
"narrative_follow":"ما الذي سيتبعه المستمع","soft_narrative_spine":"...","gentle_curiosity":"...","attentional_anchor":"...","sensory_potential_notes":"...","tension_risks":["..."],"reentry_plan":"...","long_form_capacity":"...","difference_from_recent":"...",
"scores":{{"gentle_curiosity":0,"low_arousal":0,"quest_clarity":0,"historical_immersion":0,"sensory_potential":0,"recoverability":0,"long_form_sustainability":0,"curiosity_decay_potential":0,"low_stakes":0,"variety":0}}
}}]}}
كل درجة عدد صحيح من صفر إلى خمسة. لا تُرجع مرشحًا واحدًا فقط. penalty_flags يضم ما ينطبق من: {', '.join(quest_rules.PENALTY_FLAGS)} (والمرشح الذي ينطبق عليه أي منها سيُرفض).
نكهات المهمة الأخيرة في السجل: {recent_story_formats(history) or 'لا يوجد'}؛ لا تكرر نكهة استُخدمت في آخر حلقتين إلا إذا كانت أفضل بوضوح، ولا تُنشئ مرشحين كلهم بالنكهة نفسها.
كل مرشح يجب أن يملك مهمة واضحة وافتتاحًا بمشهد (شخصية تفعل شيئًا) لا بمحاضرة، ويقبل التقسيم إلى مشاهد صغيرة مترابطة، ويُفهم دون معرفة تاريخية مسبقة، ويمكن تهدئته تدريجيًا، ويمكن للمستمع أن يسرح فيه ويعود، وألا يكون قائمة حقائق ولا روتينًا بلا هدف.
"""
    text, _ = gemini(prompt, json_mode=True, max_tokens=7000, temperature=0.7)
    discovery = parse_topic_discovery_with_one_repair(text)
    candidates = discovery.get("candidates") or []
    # Gemini may return fewer than the preferred six; what matters is enough
    # valid, unused, varied candidates once the saved bank is merged in.
    blocked = used_slugs | {
        str(item.get("slug", "")) for item in bank
        if item.get("bank_status") in {"selected", "rejected"}
    }
    fresh = [
        item for item in candidates
        if is_valid_topic_candidate(item) and safe_slug(str(item["slug"])) not in blocked
    ]
    save_topic_candidate_bank(fresh)
    combined = {str(item.get("slug", "")): item for item in saved_ideas if is_valid_topic_candidate(item)}
    combined.update({str(item.get("slug", "")): item for item in fresh})
    eligible = list(combined.values())
    if len(eligible) < MIN_VALID_CANDIDATES:
        raise RuntimeError(
            f"TOPIC_DISCOVERY_FAILED: fewer than {MIN_VALID_CANDIDATES} valid candidates "
            f"({len(fresh)} new + {len(eligible) - len(fresh)} saved)"
        )
    families = {str(item.get("family", "")).strip().casefold() for item in eligible if item.get("family")}
    if len(families) < MIN_NARRATIVE_FAMILIES:
        raise RuntimeError(
            f"TOPIC_DISCOVERY_FAILED: fewer than {MIN_NARRATIVE_FAMILIES} narrative families "
            f"({len(families)} found: {sorted(families)})"
        )
    topic = pick_topic_by_format(eligible, history)
    topic["narrative_identity"] = quest_rules.IDENTITY
    topic["slug"] = safe_slug(str(topic.get("slug", "")))
    required = (
        "topic_ar", "angle_ar", "slug", "search_query_en", "period_ar",
        "location_ar", "family", "story_format", "reason_ar", "narrative_follow",
        "soft_narrative_spine", "gentle_curiosity", "attentional_anchor",
        "sensory_potential_notes", "reentry_plan", "long_form_capacity",
        "difference_from_recent", *quest_rules.PREMISE_FIELDS,
    )
    missing = [key for key in required if not quest_rules.nonempty(topic.get(key))]
    if missing:
        raise RuntimeError(f"Topic selection is missing required fields: {missing}")
    validation_prompt = f"""{production_rules(*range(2, 17), 62, 64, 65)}

أنت بوابة مستقلة لاختيار الموضوع. قيّم المرشح (Story Premise) مقارنة بالسجل السابق، ولا تحاول إصلاحه.
المرشح: {json.dumps(topic, ensure_ascii=False)}
السجل: {prior}

أعد JSON فقط بالشكل:
{{"status":"PASS أو REJECT","language":true,"religion":true,"sleep":true,"quality":true,"universality":true,"non_duplicate":true,"soft_narrative_spine":true,"gentle_curiosity":true,"attentional_anchoring":true,"reentry_tolerance":true,"gradual_unwind":true,"long_form_without_filler":true,"clear_narrative_thread":true,"scene_openable":true,"divisible_into_scenes":true,"understandable_without_prior_knowledge":true,"gradually_calmable":true,"not_a_fact_list":true,"has_small_want":true,"low_stakes_quest":true,"not_mystery_or_thriller":true,"summarizable_in_one_sentence":true,"not_overplotted":true,"not_boring_routine":true,"verifiable_historical_world":true,"fiction_clearly_separated":true,"scores":{{"gentle_curiosity":0,"low_arousal":0,"quest_clarity":0,"historical_immersion":0,"sensory_potential":0,"recoverability":0,"long_form_sustainability":0,"curiosity_decay_potential":0,"low_stakes":0,"variety":0}},"penalty_flags":[],"reasons":["..."]}}
كل قيمة true تعني أن البوابة نجحت. اختبار: إذا كان المستمع يحتاج بشدة إلى معرفة النهاية فالحبكة أقوى من المطلوب فارفض. ارفض الألغاز والإثارة والرهانات العالية والروتين بلا هدف والموسوعية. ارفض عند الشك الجوهري أو نقص المادة والمصادر عن العالم التاريخي. penalty_flags تضم أي علامة من: {', '.join(quest_rules.PENALTY_FLAGS)} تنطبق فعلًا.
"""
    validation_text, _ = gemini(
        validation_prompt, json_mode=True, max_tokens=1200, temperature=0.0
    )
    validation = parse_json_object(validation_text, "TOPIC_VALIDATION_SCHEMA_FAILED")
    gate_names = (
        "language", "religion", "sleep", "quality", "universality", "non_duplicate",
        "soft_narrative_spine", "gentle_curiosity", "attentional_anchoring",
        "reentry_tolerance", "gradual_unwind", "long_form_without_filler",
        "clear_narrative_thread", "scene_openable", "divisible_into_scenes",
        "understandable_without_prior_knowledge", "gradually_calmable", "not_a_fact_list",
        "has_small_want", "low_stakes_quest", "not_mystery_or_thriller",
        "summarizable_in_one_sentence", "not_overplotted", "not_boring_routine",
        "verifiable_historical_world", "fiction_clearly_separated",
    )
    verified_scores = validation.get("scores") or {}
    valid_scores = all(
        isinstance(verified_scores.get(name), int) and 0 <= verified_scores[name] <= 5
        for name in SLEEP_SCORE_NAMES
    )
    score_average = (
        sum(verified_scores[name] for name in SLEEP_SCORE_NAMES) / len(SLEEP_SCORE_NAMES)
        if valid_scores else 0
    )
    if not story_format_allowed(str(topic.get("story_format", "")), history):
        save_topic_candidate_bank([], rejected_slug=topic["slug"])
        raise RuntimeError("TOPIC_GATE_REJECTED: story_format repeated three episodes in a row")
    if (
        validation.get("status") != "PASS"
        or not all(validation.get(name) is True for name in gate_names)
        or not valid_scores
        or min((verified_scores[name] for name in SLEEP_SCORE_NAMES), default=0) < quest_rules.MIN_SCORE
        or any(verified_scores.get(name, 0) < floor for name, floor in quest_rules.CRITICAL_SCORES.items())
        or score_average < quest_rules.MIN_AVERAGE
        or {str(flag).strip().upper() for flag in (validation.get("penalty_flags") or [])} & set(quest_rules.PENALTY_FLAGS)
    ):
        save_topic_candidate_bank([], rejected_slug=topic["slug"])
        raise RuntimeError(
            "TOPIC_GATE_REJECTED: " + json.dumps(validation, ensure_ascii=False)
        )
    topic["discovery_candidate_count"] = len(candidates)
    topic["discovery_family_count"] = len(families)
    topic["selection_gate"] = validation
    save_topic_candidate_bank([], selected_slug=topic["slug"])
    return topic


def choose_valid_topic(history: list[dict], attempts: int = 3) -> dict:
    last_error: RuntimeError | None = None
    for _attempt in range(attempts):
        try:
            return choose_topic(history, retry_feedback=str(last_error or ""))
        except RuntimeError as exc:
            if not any(code in str(exc) for code in ("TOPIC_GATE_REJECTED", "TOPIC_DISCOVERY_FAILED")):
                raise
            last_error = exc
    raise last_error or RuntimeError("TOPIC_GATE_REJECTED: no acceptable topic was found")


def _http_json(url: str, *, headers: dict, body: dict | None = None, timeout: int = 90) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if body is not None else "GET",
        headers={**headers, "Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def search_exa(query: str, key: str) -> list[dict]:
    data = _http_json(
        "https://api.exa.ai/search",
        headers={"x-api-key": key},
        body={"query": query, "numResults": 8, "contents": {"text": {"maxCharacters": 3000}}},
    )
    return [
        {"title": item.get("title") or item.get("url"), "url": item.get("url"), "excerpt": item.get("text") or ""}
        for item in data.get("results", [])
    ]


def search_brave(query: str, key: str) -> list[dict]:
    params = urllib.parse.urlencode({"q": query, "count": 10, "extra_snippets": "true"})
    data = _http_json(
        f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={"X-Subscription-Token": key},
    )
    return [
        {
            "title": item.get("title") or item.get("url"),
            "url": item.get("url"),
            "excerpt": " ".join([item.get("description") or "", *(item.get("extra_snippets") or [])]),
        }
        for item in (data.get("web") or {}).get("results", [])
    ]


def search_firecrawl(query: str, key: str) -> list[dict]:
    data = _http_json(
        "https://api.firecrawl.dev/v2/search",
        headers={"Authorization": f"Bearer {key}"} if key else {},
        body={"query": query[:500], "limit": 6, "scrapeOptions": {"formats": [{"type": "markdown"}]}},
        timeout=180,
    )
    return [
        {
            "title": item.get("title") or item.get("url"),
            "url": item.get("url"),
            "excerpt": item.get("markdown") or item.get("description") or "",
        }
        for item in (data.get("data") or {}).get("web", [])
    ]


# Used in order after Gemini's Google Search grounding is exhausted.
SEARCH_PROVIDERS = (
    ("exa", "EXA_API_KEY", search_exa),
    ("brave", "BRAVE_SEARCH_API_KEY", search_brave),
    ("firecrawl", "FIRECRAWL_API_KEY", search_firecrawl),
)


def _collect_sources(topic: dict) -> tuple[str, list[dict]]:
    """Return (provider, sources) from the first search provider with enough evidence."""
    secrets = load_secrets()
    queries = [
        str(topic.get("search_query_en") or "").strip(),
        f"{topic['topic_ar']} {topic['period_ar']}".strip(),
    ]
    failures = []
    for name, secret, search in SEARCH_PROVIDERS:
        key = secrets.get(secret, "")
        if not key:
            failures.append(f"{name}: no key")
            continue
        results: list[dict] = []
        try:
            for query in filter(None, queries):
                run_summary.count("search")
                for item in search(query, key):
                    url = str(item.get("url") or "")
                    if url.startswith("https://") and not any(found["url"] == url for found in results):
                        results.append(item)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
            code = getattr(error, "code", "")
            failures.append(f"{name}: {type(error).__name__} {code}".strip())
            print(f"Research search provider {name} failed ({code or type(error).__name__}); trying next", file=sys.stderr, flush=True)
            continue
        domains = {urllib.parse.urlparse(item["url"]).netloc.lower().removeprefix("www.") for item in results}
        if len(results) >= 3 and len(domains) >= 2:
            print(f"Research sources collected via {name} ({len(results)} results)", file=sys.stderr, flush=True)
            return name, results[:10]
        failures.append(f"{name}: only {len(results)} usable results")
    # Firecrawl officially permits a low-rate keyless search request. Use one
    # minimal query only after every configured free provider is unavailable or
    # insufficient; no Authorization header is sent when the key is empty.
    keyless_query = next(filter(None, queries), "")
    if keyless_query:
        try:
            run_summary.count("search")
            results = [
                item for item in search_firecrawl(keyless_query, "")
                if str(item.get("url") or "").startswith("https://")
                and str(item.get("excerpt") or "").strip()
            ]
            domains = {
                urllib.parse.urlparse(item["url"]).netloc.lower().removeprefix("www.")
                for item in results
            }
            if len(results) >= 3 and len(domains) >= 2:
                print(
                    f"Research sources collected via firecrawl_keyless ({len(results)} results; 1 query)",
                    file=sys.stderr, flush=True,
                )
                return "firecrawl_keyless", results[:10]
            failures.append(f"firecrawl_keyless: only {len(results)} usable results")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
            code = getattr(error, "code", "")
            failures.append(f"firecrawl_keyless: {type(error).__name__} {code}".strip())
    raise RuntimeError("GROUNDED_RESEARCH_UNAVAILABLE: all search providers failed (" + "; ".join(failures) + ")")


def _research_key(topic: dict) -> str:
    seed = "|".join(str(topic.get(key, "")) for key in ("topic_ar", "angle_ar", "period_ar", "search_query_en"))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def research_from_search_providers(topic: dict, prompt: str, cache_path: Path | None = None) -> tuple[str, list[dict]]:
    cached = _read_json(cache_path, {}) if cache_path else {}
    if cached.get("topic_key") == _research_key(topic) and cached.get("results"):
        # Sources already collected by an earlier attempt are never thrown away.
        provider, results = cached["provider"], cached["results"]
        print(f"Reusing {len(results)} previously collected research sources ({provider})", file=sys.stderr, flush=True)
    else:
        provider, results = _collect_sources(topic)
        if cache_path:
            write(cache_path, json.dumps(
                {"topic_key": _research_key(topic), "provider": provider, "results": results},
                ensure_ascii=False, indent=2))
    run_summary.set_search_provider(provider)
    evidence = "\n\n".join(
        f"[{index}] {item['title']}\n{item['url']}\n{' '.join(str(item['excerpt']).split())[:2500]}"
        for index, item in enumerate(results, 1)
    )
    grounded_prompt = (
        prompt
        + "\n\nاعتمد حصريًا على المصادر المرقمة التالية التي جُمعت من البحث على الإنترنت. "
        "لا تضف أي معلومة غير موجودة فيها، وأشر إلى المصدر برقمه بين قوسين مثل [1] بجانب كل ادعاء مهم وفي «خريطة الادعاءات».\n\n"
        + evidence
    )
    research, _ = gemini(grounded_prompt, max_tokens=12000, temperature=0.2)
    # The model cites by number; the reference list maps each [n] to its real URL,
    # so the notes always carry direct, verifiable links.
    references = "\n".join(f"[{index}] {item['title']} — {item['url']}" for index, item in enumerate(results, 1))
    research = f"{research.rstrip()}\n\n## المصادر\n{references}\n"
    sources = [{"title": item["title"], "url": item["url"], "provider": provider} for item in results]
    return research, sources


def research_topic(topic: dict, story: Path | None = None) -> tuple[str, list[dict]]:
    rules = production_rules(18, 19, 20, 21)
    prompt = f"""التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

ابحث بحثًا تاريخيًا موثقًا للحلقة التالية:
الموضوع: {topic['topic_ar']}
الزاوية: {topic['angle_ar']}
الفترة: {topic['period_ar']}

استخدم مصادر أولية أو موثوقة قدر الإمكان: UNESCO، متاحف، جامعات، دوريات وأبحاث أثرية، ومؤسسات تراث رسمية. ويكيبيديا للاكتشاف فقط وليست المصدر الوحيد.
اكتب ملاحظات بحث عربية مفصلة ومنظمة تشمل: المكان والزمن، البيئة، البيوت، الطعام، الملابس، الحرف، التجارة، النقل، الإدارة، المعتقدات اللازمة للفهم، الأدلة الأثرية، التسلسل التاريخي، وما هو مختلف عليه.
أنشئ في النهاية قسمًا بعنوان «خريطة الادعاءات» يربط الادعاءات المهمة بالمصادر المذكورة. لا تخترع أسماء أو تواريخ أو اقتباسات. ضع روابط المصادر صراحة داخل النص أيضًا.
"""
    try:
        research, sources = gemini(prompt, search=True, max_tokens=12000, temperature=0.2)
    except RuntimeError as error:
        if "GROUNDED_RESEARCH_UNAVAILABLE" not in str(error) and "daily quota" not in str(error).lower():
            raise
        print(f"Google Search grounding unavailable ({str(error)[:160]}); using search providers", file=sys.stderr, flush=True)
        cache = story / "logs" / "research_search_results.json" if story else None
        research, sources = research_from_search_providers(topic, prompt, cache)
    valid_sources = []
    domains = set()
    for source in sources:
        url = str(source.get("url", "")).strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "https" and parsed.netloc:
            valid_sources.append(source)
            domains.add(parsed.netloc.lower().removeprefix("www."))
    if len(valid_sources) < 3 or len(domains) < 2:
        raise RuntimeError(
            "SOURCE_VALIDATION_FAILED: grounded research requires at least "
            "3 sources across 2 independent domains"
        )
    if "خريطة الادعاءات" not in research:
        raise RuntimeError("SOURCE_VALIDATION_FAILED: research is missing a claim map")
    if len(re.findall(r"https://[^\s)>]+", research)) < 3:
        raise RuntimeError("SOURCE_VALIDATION_FAILED: research notes lack direct source URLs")
    return research, valid_sources


STORY_QUALITY_ENGAGEMENT_GATES = (
    "first_listen_comprehension", "teen_first_listen_comprehension", "calm_hook",
    "clear_narrative_thread", "scene_based_storytelling", "one_main_idea_per_scene",
    "terms_explained", "name_date_density", "clear_transitions", "adult_not_childish",
    "historical_grounding", "fiction_clearly_separated", "not_boring", "not_overstimulating",
    # IMMERSIVE LOW-STAKES HISTORICAL QUEST (rule 65): questions A-O of the final test.
    "reason_to_follow", "clear_quest", "low_stakes_quest", "movement_and_progress",
    "history_embedded_in_scenes", "curiosity_gentle_not_urgent", "can_sleep_before_the_end",
    "density_drops_toward_end", "last_fifth_calmer_than_first_half", "recoverable_after_drifting",
    "sensory_detail", "not_boring_routine", "low_arousal", "verified_historical_world",
    "summarizable_in_one_sentence", "not_overplotted", "no_late_new_question",
    "quiet_post_resolution",
)


class StoryQualityGateRejected(RuntimeError):
    """Structured quality rejection so the caller can repair only the failing scope."""

    def __init__(self, details: dict):
        self.details = details
        super().__init__("STORY_QUALITY_GATE_REJECTED: " + json.dumps(details, ensure_ascii=False))


def validate_story_quality(topic: dict, story_text: str) -> dict:
    deterministic = quest_rules.story_text_errors(story_text)
    if deterministic:
        raise StoryQualityGateRejected({"deterministic": deterministic})
    quest = {
        key: topic.get(key) for key in (
            "character_ar", "protagonist_kind", "small_want_ar", "quest_ar", "one_sentence_summary_ar",
        ) if topic.get(key)
    }
    gate_json = ",".join(f'"{name}":true' for name in STORY_QUALITY_ENGAGEMENT_GATES)
    prompt = f"""{production_rules(2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 23, 62, 64, 65)}

أنت بوابة جودة مستقلة. افحص النص النهائي دون إعادة كتابته.
الموضوع: {topic['topic_ar']} — {topic['angle_ar']}
المهمة المصممة: {json.dumps(quest, ensure_ascii=False)}

أعد JSON فقط:
{{"status":"PASS أو REJECT","language":true,"adult_audience":true,"religion":true,"sleep":true,"historical_style":true,"no_dangerous_drift":true,"soft_narrative_spine":true,"gentle_curiosity":true,"attentional_anchoring":true,"sensory_imagery":true,"low_stakes":true,"gradual_unwind":true,"reentry_tolerance":true,"not_boring_or_overstimulating":true,"flexible_composition":true,{gate_json},"issues":["..."]}}
كل قيمة true تعني نجاح البوابة. الهدف: حكاية تاريخية غامرة ذات مهمة لطيفة منخفضة المخاطر. المستمع يريد أن يبقى داخل العالم لا أن يحتاج إلى معرفة النهاية. ارفض النص الممل (معلومات بلا حركة، وصف ساكن، موسوعي، قوائم، روتين بلا هدف، بطء شديد من البداية، بلا مهمة أو فضول) وارفض النص المحفز (حماس، تشويق، لغز، خطر، cliffhanger، صراع مستمر، شخصيات كثيرة، معلومات معقدة، ذروة، twist، سؤال كبير مفتوح في آخر 10%). teen_first_listen_comprehension: هل يفهمه مستمع عربي عمره 15–16 بلا معرفة سابقة من أول استماع بنبرة بالغة؟ calm_hook: هل تتضمن أول 500–700 كلمة مكانًا وزمنًا مبسطًا وشخصية وما تفعله والمهمة الصغيرة ولماذا نتابعها بمشهد لا بمقدمة موسوعية؟ fiction_clearly_separated: الشخصية الخيالية المركّبة مسموحة لكن لا تُقدَّم كشخصية تاريخية حقيقية، ولا تُخترع حوارات أو أفكار أو أفعال خاصة لشخصية تاريخية حقيقية، ولا تُستخدم الحبكة لتمرير تفاصيل تاريخية غير موثقة. verified_historical_world: تفاصيل العالم (الأسواق والحرف والمواد...) مما تدعمه المصادر أو مصوغة بحذر. gentle_curiosity وreentry_tolerance وgradual_unwind وdensity_drops_toward_end وlast_fifth_calmer_than_first_half تُفحص عبر أثلاث النص (آخر 20% يجب أن يكون أهدأ بوضوح من أول 50%). can_sleep_before_the_end: هل يستطيع المستمع أن ينام قبل النهاية دون أن يفقد شيئًا مهمًا؟ quiet_post_resolution: هل تُحل المهمة قبل النهاية بوقت كافٍ ثم تأتي فترة هادئة بعد الحل؟ ارفض إذا كانت اللغة ثقيلة أو طفولية، أو أصبح المحتوى دينيًا بحتًا، أو صادمًا وغير مناسب للنوم.

النص:
{story_text[:90000]}
"""
    text, _ = gemini(prompt, json_mode=True, max_tokens=4000, temperature=0.0)
    result = parse_json_object(text, "STORY_QUALITY_SCHEMA_FAILED")
    gates = (
        "language", "adult_audience", "religion", "sleep", "historical_style",
        "no_dangerous_drift", "soft_narrative_spine", "gentle_curiosity",
        "attentional_anchoring", "sensory_imagery", "low_stakes", "gradual_unwind",
        "reentry_tolerance", "not_boring_or_overstimulating", "flexible_composition",
        *STORY_QUALITY_ENGAGEMENT_GATES,
    )
    if result.get("status") != "PASS" or not all(result.get(name) is True for name in gates):
        raise StoryQualityGateRejected(result)
    return result


def _story_split_at_fraction(text: str, fraction: float) -> tuple[str, str]:
    tokens = list(re.finditer(r"\S+", text))
    if len(tokens) < 2:
        return "", text
    index = max(1, min(len(tokens) - 1, int(len(tokens) * fraction)))
    cut = tokens[index].start()
    return text[:cut].rstrip(), text[cut:].lstrip()


def _quality_repair_scope(details: dict) -> str | None:
    deterministic = [str(item) for item in (details.get("deterministic") or [])]
    issues = [str(item) for item in (details.get("issues") or [])]
    false_gates = [str(key) for key, value in details.items() if value is False]
    signal = " ".join([*deterministic, *issues, *false_gates]).lower()

    if any(token in signal for token in (
        "encyclopedic opening", "calm_hook", "first_listen_comprehension",
        "teen_first_listen_comprehension",
    )):
        return "opening"
    if any(token in signal for token in (
        "curiosity does not decay", "mystery", "twist", "shock", "cliffhanger",
        "ending", "gradual_unwind", "density_drops_toward_end",
        "last_fifth_calmer_than_first_half", "no_late_new_question",
        "quiet_post_resolution", "can_sleep_before_the_end",
    )):
        return "ending"
    # Semantic quality failures can still be repaired, but only after the gate has
    # named concrete issues. A bare rejection is not enough evidence to rewrite.
    if issues or false_gates:
        return "full"
    return None


def _is_late_question_failure(details: dict) -> bool:
    signal = " ".join([
        *(str(item) for item in (details.get("deterministic") or [])),
        *(str(item) for item in (details.get("issues") or [])),
        *(str(key) for key, value in details.items() if value is False),
    ]).lower()
    return "curiosity does not decay" in signal or "no_late_new_question" in signal


def _question_sentence_spans(text: str) -> list[tuple[int, int, str]]:
    pattern = re.compile(r"(?:^|(?<=[.!؟?\n]))\s*([^.!؟?\n]*[؟?])", re.MULTILINE)
    return [
        (match.start(1), match.end(1), match.group(1).strip())
        for match in pattern.finditer(text)
        if match.group(1).strip()
    ]


def _repair_late_questions(topic: dict, target: str, details: dict) -> str:
    """Repair only late question sentences; every other character stays untouched."""
    spans = _question_sentence_spans(target)
    if not spans:
        raise RuntimeError("STORY_QUALITY_REPAIR_FAILED: no late question sentences were found")
    prompt = f"""حوّل فقط الجمل الاستفهامية المتأخرة التالية إلى جمل خبرية هادئة تحمل المعنى نفسه قدر الإمكان.
لا تضف حقيقة أو اسمًا أو حدثًا جديدًا، ولا تستخدم علامة استفهام في البدائل.
أعد JSON فقط:
{{"replacements":[{{"index":0,"text":"..."}}]}}

الموضوع: {topic.get('topic_ar', '')}
سبب الإصلاح: {json.dumps(details, ensure_ascii=False)}
الجمل:
{json.dumps([{"index": i, "text": span[2]} for i, span in enumerate(spans)], ensure_ascii=False)}
"""
    raw, _ = gemini(prompt, json_mode=True, max_tokens=3000, temperature=0.0)
    result = parse_json_object(raw, "STORY_QUALITY_REPAIR_SCHEMA_FAILED")
    replacements = result.get("replacements")
    if not isinstance(replacements, list):
        raise RuntimeError("STORY_QUALITY_REPAIR_FAILED: replacements must be a list")
    mapped: dict[int, str] = {}
    for item in replacements:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        replacement = str(item.get("text", "")).strip()
        if 0 <= index < len(spans) and replacement and not re.search(r"[؟?]", replacement):
            if replacement[-1:] not in ".!…":
                replacement += "."
            mapped[index] = replacement
    if len(mapped) != len(spans):
        raise RuntimeError(
            f"STORY_QUALITY_REPAIR_FAILED: expected {len(spans)} replacements, got {len(mapped)}"
        )
    repaired = target
    for index in range(len(spans) - 1, -1, -1):
        start, end, _ = spans[index]
        repaired = repaired[:start] + mapped[index] + repaired[end:]
    return repaired


def _quality_repair_prompt(
    *, topic: dict, research: str, scope: str, details: dict,
    target_text: str, context_before: str = "",
) -> str:
    common = f"""أنت مصلح جودة محدود الصلاحية لقصة نوم تاريخية.
سبب الرفض:
{json.dumps(details, ensure_ascii=False)}

الموضوع: {topic.get('topic_ar', '')}
الزاوية: {topic.get('angle_ar', '')}
المهمة:
{quest_brief(topic)}

قواعد إلزامية:
- أصلح فقط سبب الرفض المذكور، ولا تعِد اختراع القصة من الصفر.
- لا تضف حقيقة تاريخية جديدة ولا اسمًا جديدًا ولا تاريخًا جديدًا.
- حافظ على ترتيب الأحداث والحقائق والشخصية والمهمة الموجودة.
- لا تختصر النص اختصارًا كبيرًا ولا تضف حشوًا.
- لا تضف عنوانًا أو ملاحظات تحريرية.
- أخرج النص المصحح فقط.
"""
    if scope == "ending":
        return common + f"""
هذه هي نهاية القصة فقط. أعد كتابة هذا المقطع وحده ليصبح أهدأ بوضوح:
- المهمة تكون محلولة قبل النهاية بوقت كافٍ.
- لا تفتح أي سؤال جديد ولا تستخدم علامة استفهام إلا إذا كانت جزءًا لا يمكن تجنبه من كلام منقول، والأفضل تحويله إلى جملة تقريرية.
- لا لغز ولا مفاجأة ولا teaser ولا cliffhanger.
- قلل الحوار والحركة الجديدة، واجعل آخر المقاطع أعمالًا مألوفة وعودة/ترتيب/إغلاق/هدوء.
- حافظ على الحقائق والأحداث الموجودة في هذا المقطع دون إضافة ادعاءات جديدة.

سياق سابق لا تعِد كتابته:
{context_before[-2500:]}

المقطع المطلوب إصلاحه:
{target_text}

مرجع البحث للاستخدام عند الحاجة فقط، لا لإضافة معلومات جديدة:
{research[:24000]}
"""
    if scope == "opening":
        return common + f"""
هذه هي بداية القصة فقط. أعد كتابة هذا المقطع وحده:
- ابدأ مباشرة بمشهد وشخصية تفعل شيئًا.
- عرّف المكان والزمن والمهمة الصغيرة تدريجيًا، لا كمقدمة موسوعية.
- لا ترحيب، لا تقديم للقناة، لا تعليمات تنفس.
- حافظ على كل الحقائق الموجودة ولا تضف ادعاءات تاريخية جديدة.

المقطع المطلوب إصلاحه:
{target_text}

ما يأتي بعده للسياق فقط:
{context_before[:1800]}

مرجع البحث:
{research[:24000]}
"""
    return common + f"""
أصلح القصة كاملة بأقل تغيير ممكن لمعالجة القضايا المذكورة فقط.
لا تغيّر أي جزء لا يحتاج إصلاحًا. يجب أن تبقى القصة 5000 كلمة عربية على الأقل.

القصة:
{target_text[:90000]}

مرجع البحث:
{research[:30000]}
"""


def repair_story_quality(
    story: Path,
    topic: dict,
    research: str,
    story_text: str,
    details: dict,
    *,
    attempt: int,
) -> str:
    scope = _quality_repair_scope(details)
    if scope is None:
        raise StoryQualityGateRejected(details)

    if scope == "ending":
        preserved, target = _story_split_at_fraction(story_text, 0.75)
        context = preserved
        max_tokens = 7000
    elif scope == "opening":
        target, preserved = _story_split_at_fraction(story_text, 0.18)
        context = preserved
        max_tokens = 5000
    else:
        preserved, target, context = "", story_text, ""
        max_tokens = 16000

    original_target_words = word_count(target)
    if scope == "ending" and _is_late_question_failure(details):
        repaired = _repair_late_questions(topic, target, details).strip()
        repaired_target_words = word_count(repaired)
        if abs(repaired_target_words - original_target_words) > max(40, int(original_target_words * 0.08)):
            raise RuntimeError(
                f"STORY_QUALITY_REPAIR_FAILED: late-question repair changed length too much "
                f"({original_target_words} -> {repaired_target_words})"
            )
    else:
        prompt = _quality_repair_prompt(
            topic=topic, research=research, scope=scope, details=details,
            target_text=target, context_before=context,
        )
        repaired, _ = gemini(prompt, max_tokens=max_tokens, temperature=0.15)
        repaired = repaired.strip()
        if not repaired:
            raise RuntimeError("STORY_QUALITY_REPAIR_FAILED: model returned empty text")
        repaired_target_words = word_count(repaired)
        if repaired_target_words < max(250, int(original_target_words * 0.70)):
            raise RuntimeError(
                f"STORY_QUALITY_REPAIR_FAILED: {scope} repair shrank from "
                f"{original_target_words} to {repaired_target_words} Arabic words"
            )

    if scope == "ending":
        candidate = f"{preserved.rstrip()}\n\n{repaired}"
    elif scope == "opening":
        candidate = f"{repaired}\n\n{preserved.lstrip()}"
    else:
        candidate = repaired

    if word_count(candidate) < 5000:
        raise RuntimeError(
            f"STORY_QUALITY_REPAIR_FAILED: repaired story has {word_count(candidate)} Arabic words"
        )

    logs = story / "logs"
    write(logs / f"quality_repair_{attempt:02d}_{scope}.md", repaired)
    write(logs / f"quality_repair_{attempt:02d}_{scope}.json", json.dumps({
        "scope": scope, "details": details,
        "before_words": word_count(story_text), "after_words": word_count(candidate),
    }, ensure_ascii=False, indent=2))
    return candidate


def ensure_story_quality(
    story: Path,
    topic: dict,
    research: str,
    story_text: str,
    *,
    label: str,
) -> tuple[str, dict]:
    candidate = story_text
    for attempt in range(1, STORY_QUALITY_REPAIR_ATTEMPTS + 2):
        try:
            return candidate, validate_story_quality(topic, candidate)
        except StoryQualityGateRejected as exc:
            if attempt > STORY_QUALITY_REPAIR_ATTEMPTS:
                raise
            print(
                f"QUALITY_TARGETED_REPAIR attempt {attempt}/{STORY_QUALITY_REPAIR_ATTEMPTS} "
                f"label={label} scope={_quality_repair_scope(exc.details)}",
                file=sys.stderr, flush=True,
            )
            candidate = repair_story_quality(
                story, topic, research, candidate, exc.details, attempt=attempt
            )
    raise RuntimeError("STORY_QUALITY_REPAIR_FAILED: unreachable")


def _fact_check_json(prompt: str, *, max_tokens: int) -> dict:
    last_shape = "unknown"
    for attempt in range(2):
        retry_instruction = "" if attempt == 0 else (
            "\n\nتصحيح إلزامي: أعد كائن JSON واحدًا فقط مطابقًا للمخطط المطلوب، "
            "ولا تُعد قائمة JSON."
        )
        text, _ = gemini(
            prompt + retry_instruction,
            json_mode=True,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        try:
            return parse_json_object(text, "FACT_CHECK_SCHEMA_FAILED")
        except ValueError as exc:
            last_shape = str(exc).split("got ", 1)[-1]
    raise RuntimeError(
        f"FACT_CHECK_SCHEMA_FAILED: expected one JSON object, got {last_shape}"
    )


def fact_check_story(topic: dict, research: str, story_text: str) -> tuple[str, dict]:
    rules = production_rules(26, 27, 28, 29)
    first_prompt = f"""{rules}

نفّذ Fact Check مستقلًا اعتمادًا فقط على البحث وخريطة الادعاءات. فرّق بين نوعين:
(أ) ادعاءات العالم التاريخي (المدينة والأسواق والحرف والمواد والأدوات والطعام والنقل والعمارة والتجارة والعادات): تحتاج دعمًا من البحث.
(ب) أحداث الحكاية الخيالية (ما تفعله الشخصية الخيالية المركّبة وأقوالها): لا تحتاج مصدرًا ولا تبحث عن مصدر يثبتها، لكن يجب أن تكون ممكنة تاريخيًا.
ممنوع: تفصيلة تاريخية غير موثقة مُمرَّرة داخل الحكاية (fiction_smuggling)، وشخصية خيالية مقدَّمة كأنها شخصية تاريخية حقيقية، واختراع حوار أو أفكار أو أفعال لشخصية حقيقية وتقديمها كحقيقة. أدرج هذه في unsupported_critical.
أعد JSON فقط:
{{"status":"PASS أو NEEDS_CORRECTION","verified":["..."],"uncertain":[{{"claim":"...","action":"rephrase أو remove","replacement":"..."}}],"corrected":[{{"original":"...","replacement":"...","source":"URL أو مرجع من البحث"}}],"unsupported_critical":["..."],"fiction_smuggling":["..."],"fictional_presented_as_documented":["..."]}}

الموضوع: {topic['topic_ar']}
البحث:
{research[:50000]}

القصة:
{story_text[:90000]}
"""
    first = _fact_check_json(first_prompt, max_tokens=8000)
    uncertain = first.get("uncertain") or []
    corrected = first.get("corrected") or []
    unsupported = [
        *(first.get("unsupported_critical") or []),
        *(first.get("fiction_smuggling") or []),
        *(first.get("fictional_presented_as_documented") or []),
    ]
    final_story = story_text
    if corrected or uncertain or unsupported or first.get("status") != "PASS":
        correction_prompt = f"""{rules}

طبّق حصراً التصحيحات التالية على القصة كاملة. عالج UNCERTAIN بإعادة صياغة حذرة أو الحذف حسب action، واحذف الادعاءات الحرجة غير المدعومة. لا تضف أي معلومة جديدة ولا تغير النبرة أو بنية القصة خارج المواضع اللازمة. أخرج القصة المصححة فقط بلا Markdown إضافي.

CORRECTED:
{json.dumps(corrected, ensure_ascii=False)}
UNCERTAIN:
{json.dumps(uncertain, ensure_ascii=False)}
UNSUPPORTED:
{json.dumps(unsupported, ensure_ascii=False)}

القصة:
{story_text[:90000]}
"""
        final_story, _ = gemini(correction_prompt, max_tokens=16000, temperature=0.0)
        if word_count(final_story) < 5000:
            raise RuntimeError("FACT_CORRECTION_FAILED: corrected story became too short")

    second_prompt = f"""{rules}

هذا هو التحقق النهائي الثاني بعد تطبيق التصحيحات. اعتمد فقط على البحث. تحقق من ادعاءات العالم التاريخي فقط؛ أحداث الحكاية الخيالية لا تحتاج مصدرًا لكنها يجب ألا تُمرّر تفاصيل تاريخية غير موثقة ولا تُقدَّم الشخصية الخيالية كحقيقية. أعد JSON فقط:
{{"status":"PASS أو FAIL","remaining_errors":[],"unsupported_critical":[],"uncertain_presented_as_fact":[],"fiction_smuggling":[],"fictional_presented_as_documented":[],"corrections_applied":true}}

البحث:
{research[:50000]}

القصة النهائية:
{final_story[:90000]}
"""
    second = _fact_check_json(second_prompt, max_tokens=5000)
    passed = (
        second.get("status") == "PASS"
        and second.get("corrections_applied") is True
        and not (second.get("remaining_errors") or [])
        and not (second.get("unsupported_critical") or [])
        and not (second.get("uncertain_presented_as_fact") or [])
        and not (second.get("fiction_smuggling") or [])
        and not (second.get("fictional_presented_as_documented") or [])
    )
    if not passed:
        raise RuntimeError("FINAL_FACT_CHECK_FAILED: " + json.dumps(second, ensure_ascii=False))
    return final_story, {"pass_1": first, "pass_2": second, "status": "PASS"}


def finalize_story_text(story: Path, topic: dict, research: str, draft: str) -> tuple[str, dict, dict]:
    logs = story / "logs"
    draft_hash = hashlib.sha256(draft.encode("utf-8")).hexdigest()
    manifest_path = logs / "content_gates.json"
    final_path = logs / "final_story_after_gates.md"
    if manifest_path.exists() and final_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("draft_sha256") == draft_hash
            and manifest.get("quality_gate", {}).get("status") == "PASS"
            and manifest.get("fact_check", {}).get("status") == "PASS"
        ):
            return final_path.read_text(encoding="utf-8").strip(), manifest["quality_gate"], manifest["fact_check"]
    # One final quality stage only: fact-check first, then validate the exact prose
    # that will be narrated. The quality stage gets one local repair attempt.
    final_story, fact_check = fact_check_story(topic, research, draft)
    before_quality_repair = final_story
    final_story, final_quality = ensure_story_quality(
        story, topic, research, final_story, label="final"
    )
    # If that single quality repair changed prose, verify facts once more. Do not
    # start another repair cycle: a remaining quality failure stops with checkpoint.
    if final_story != before_quality_repair:
        final_story, fact_check = fact_check_story(topic, research, final_story)
        final_quality = validate_story_quality(topic, final_story)
    write(final_path, final_story)
    write(manifest_path, json.dumps({
        "draft_sha256": draft_hash,
        "final_sha256": hashlib.sha256(final_story.encode("utf-8")).hexdigest(),
        "quality_gate": final_quality,
        "fact_check": fact_check,
    }, ensure_ascii=False, indent=2))
    return final_story, final_quality, fact_check


def quest_brief(topic: dict) -> str:
    """The episode's premise as the outline and the chapters must see it (empty fields are skipped)."""
    labels = (
        ("character_ar", "الشخصية"), ("protagonist_kind", "نوع الشخصية"), ("small_want_ar", "الرغبة الصغيرة"),
        ("quest_ar", "المهمة"), ("movement_ar", "سبب الحركة عبر العالم"),
        ("immersion_opportunities", "فرص الانغماس التاريخي"), ("low_stakes_obstacles", "عقبات منخفضة الرهان"),
        ("quiet_resolution_ar", "الحل الهادئ"), ("one_sentence_summary_ar", "الخلاصة بجملة"),
    )
    lines = []
    for key, label in labels:
        value = topic.get(key)
        if quest_rules.nonempty(value):
            lines.append(f"- {label}: {json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value}")
    return "\n".join(lines) or "- (لا premise محفوظ: ابنِ مهمة صغيرة لطيفة من الزاوية نفسها)"


def make_outline(topic: dict, research: str) -> list[dict]:
    rules = production_rules(17, 22, 62, 64, 65)
    prompt = f"""التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

أنشئ مخططًا من 7 إلى 10 أقسام كبرى (اختر العدد المناسب للموضوع، لا عددًا ثابتًا) لحكاية تاريخية عربية غامرة ذات مهمة لطيفة منخفضة المخاطر، تجذب الانتباه أولًا ثم تهدأ تدريجيًا حتى النوم، عن:
{topic['topic_ar']} — {topic['angle_ar']}
نكهة المهمة: {topic.get('story_format', '')}
Premise الحلقة (لا تغيّره):
{quest_brief(topic)}

ملاحظات البحث (هي مصدر العالم التاريخي فقط؛ الشخصية الخيالية والمهمة لا تحتاجان مصدرًا لكن يجب أن تكونا ممكنتين تاريخيًا، ولا تُدخل تفصيلة تاريخية غير مدعومة بالبحث):
{research[:45000]}

اجعل الشخصية تتحرك عبر العالم بسبب المهمة: محاولات وتوقفات واكتشافات صغيرة، والتاريخ يدخل من خلال ما تراه الشخصية وتستخدمه وتحتاجه لا كمحاضرة. أقسام المخطط تغطي المراحل الخمس بهذا الترتيب: CALM_HOOK (أول قسم، نحو 10–15%: أين نحن ومتى ومن الشخصية وماذا تفعل والمهمة الصغيرة ولماذا نتابعها، بمشهد لا بمقدمة موسوعية) ثم EXPLORATION (أعلى اندماج، عدة micro-curiosity loops) ثم SETTLE (اقتراب المهمة من الحل بلا حبكة جديدة كبيرة) ثم UNWIND (أعمال مألوفة بلا أسئلة جديدة) ثم SOFT_FADE (آخر القصة: المهمة حُلَّت قبل ذلك بوقت كافٍ ونعيش فترة هادئة بعد الحل). لا ذروة ولا twist ولا cliffhanger ولا teaser ولا ملخص مدرسي ولا شكر. لا لغز ولا خطر ولا countdown ولا مطاردة ولا مؤامرة. شخصيات مسماة قليلة (ستة كحد أقصى). كل قسم كبير يحوي من 2 إلى 4 مشاهد صغيرة مترابطة، في كل مشهد فكرة رئيسية واحدة: مكان أو مرساة ← فعل ← تفصيل ← فضول صغير ← انتقال. ينخفض فتح الأسئلة الجديدة تدريجيًا: new_questions_opened أقل في الثلث الأخير منه في الأول، وصفر في UNWIND وSOFT_FADE وواحد على الأكثر في SETTLE. يقل الحوار تدريجيًا (dialogue_level: none أو light في UNWIND وSOFT_FADE). quest_state يتدرج: introduced ثم in_progress ثم nearly_solved ثم resolved (قبل القسم الأخير)، والقسم الأخير aftermath.
أعد JSON فقط:
{{"sections":[{{"title":"...","phase":"CALM_HOOK","brief":"...","quest_beat":"ما الذي تحاوله الشخصية الآن","quest_state":"introduced","new_questions_opened":1,"dialogue_level":"light","named_characters":["..."],"scenes":[{{"place_or_anchor":"...","action":"...","main_idea":"...","gentle_question":"...","transition":"..."}}],"target_words":700}}]}}
اجعل الأقسام غير متكررة، ومجموع target_words لا يقل عن 5500 كلمة عربية بلا حشو (ولا حشو للوصول إلى الطول).
"""
    feedback = ""
    last_error = ""
    for attempt in range(1, OUTLINE_MAX_MODEL_CALLS + 1):
        # A retry never resends the previous raw response: only a compact structural
        # summary and the complete list of validation errors.
        final = attempt == OUTLINE_MAX_MODEL_CALLS
        text, _ = gemini(prompt + feedback, json_mode=True, max_tokens=6000, temperature=0.5 if final else 0.3)
        try:
            result = parse_json_object(text, "OUTLINE_SCHEMA_FAILED")
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            print(f"OUTLINE_ATTEMPT={attempt} PARSE=FAIL VALIDATION=NOT_RUN ERROR={last_error}", file=sys.stderr, flush=True)
            feedback = (
                f"\n\nPrevious outline response was invalid JSON: {last_error}. Return one complete valid "
                "JSON object only. Do not use Markdown, code fences, commentary, or text outside the JSON object."
            )
            continue
        if not isinstance(result, dict):
            errors = ["Outline response must be a JSON object with a sections list"]
            sections = None
            repairs: list[str] = []
        else:
            sections, repairs = outline_repair.repair_outline(
                result.get("sections") or [], phases=STORY_PHASES,
                min_sections=MIN_MACRO_SECTIONS, max_sections=MAX_MACRO_SECTIONS,
                min_scenes=2, max_scenes=4, min_total_words=MIN_OUTLINE_TARGET_WORDS,
            )
            errors = collect_outline_errors(sections)
        if repairs:
            print(f"OUTLINE_LOCAL_REPAIRS={len(repairs)} {' | '.join(repairs)[:600]}", file=sys.stderr, flush=True)
        if not errors:
            print(f"OUTLINE_ATTEMPT={attempt} PARSE=PASS VALIDATION=PASS ERROR=", file=sys.stderr, flush=True)
            return sections
        last_error = "; ".join(errors)[:600]
        print(f"OUTLINE_ATTEMPT={attempt} PARSE=PASS VALIDATION=FAIL ERROR={last_error}", file=sys.stderr, flush=True)
        summary = json.dumps([
            {"n": number, "title": str(section.get("title", ""))[:60], "phase": section.get("phase"),
             "scenes": len(section.get("scenes") or []) if isinstance(section.get("scenes"), list) else 0,
             "target_words": section.get("target_words")}
            for number, section in enumerate(sections if isinstance(sections, list) else [], 1)
            if isinstance(section, dict)
        ], ensure_ascii=False)
        feedback = (
            f"\n\nPrevious outline failed validation: {last_error}.\n"
            f"VALIDATION_ERRORS = {json.dumps(errors, ensure_ascii=False)}\n"
            f"Previous outline structure (for reference): {summary}\n"
            "Return one complete corrected outline that fixes ALL of the errors above at once and matches "
            "the required schema exactly."
        )
    raise RuntimeError(f"OUTLINE_GENERATION_FAILED_AFTER_RETRIES: {last_error}")


def draft_story(topic: dict, research: str, sections: list[dict], log_dir: Path) -> str:
    rules = production_rules(2, 3, 4, 5, 6, 7, 17, 23, 61, 62, 64, 65)
    chapters: list[str] = []
    for index, section in enumerate(sections, 1):
        previous = chapters[-1][-2500:] if chapters else "هذه بداية السرد."
        chapter_path = log_dir / f"chapter_{index:02d}.md"
        target_words = max(850, int(section.get("target_words", 700)))
        preferred_words = chapter_preferred_words(target_words)
        prompt = f"""التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

اكتب القسم {index} من {len(sections)} من حكاية تاريخية غامرة طويلة ذات مهمة لطيفة منخفضة المخاطر للنوم.
الموضوع: {topic['topic_ar']}
الزاوية: {topic['angle_ar']}
Premise الحلقة:
{quest_brief(topic)}
عنوان القسم: {section.get('title', '')}
المرحلة: {section.get('phase', '')}
حالة المهمة: {section.get('quest_state', '')} — ما تحاوله الشخصية: {section.get('quest_beat', '')}
أسئلة جديدة تُفتح في هذا القسم: {section.get('new_questions_opened', '')} — الحوار: {section.get('dialogue_level', '')}
المطلوب: {section.get('brief', '')}
المشاهد (اكتبها بالترتيب؛ مشهد أولًا ثم المعلومة، وفكرة رئيسية واحدة لكل مشهد، ولا تحوّل الـ brief إلى مقال):
{json.dumps(section.get('scenes', []), ensure_ascii=False, indent=1)}
الهدف: بين {target_words} و{target_words + 150} كلمة عربية.

المصدر المعرفي:
{research[:42000]}

نهاية القسم السابق للسياق فقط:
{previous}

اكتب السرد وحده بلا عنوان للقسم داخل القصة وبلا ملاحظات تحريرية. فصحى سهلة بجمل منطوقة قصيرة وواضحة، بنبرة بالغة غير طفولية، دافئة ومتصلة، وتزداد هدوءًا بحسب المرحلة. ابدأ بمشهد وحافظ على الشخصية والمهمة الواحدة؛ التاريخ يدخل لأن الشخصية ترى أو تستخدم أو تحتاج أو تمر بشيء، لا كقائمة ولا كمحاضرة. قلّل الأسماء والتواريخ (أكثر هدوءًا كلما اقتربنا من النهاية)، واشرح كل مصطلح جديد فورًا بجملة قصيرة أو احذفه، واربط المشاهد بانتقالات واضحة مع إعادة سياق خفيفة (من هي الشخصية وأين وما المهمة) ليسهل الرجوع بعد شرود. حوار قصير طبيعي عند الحاجة يقل في النصف الأخير ولا ألغاز فيه. الشخصية الخيالية المركّبة مسموحة لكن لا تُقدَّم كشخصية تاريخية حقيقية، ولا تخترع حوارات أو أفكارًا أو أفعالًا لشخصية تاريخية حقيقية. العالم المحيط (الأماكن والحرف والمواد والأدوات والطعام...) من المصدر المعرفي فقط؛ لا تخترع تفصيلة تاريخية غير مدعومة لتحسين الحبكة. صِغ المسائل المختلف عليها بحذر. فضول لطيف بلا لغز ولا cliffhanger ولا ذروة ولا twist. تجنب الإثارة والدموية والخاتمة من نوع «شكرًا على الاستماع».
"""
        if chapter_path.exists():
            chapter = chapter_path.read_text(encoding="utf-8").strip()
        else:
            chapter, _ = gemini(prompt, max_tokens=7000, temperature=0.45)
            # Save the first usable response before any optional expansion. A
            # hosted runner can then restore and expand it after a transient
            # provider failure instead of generating the chapter again.
            write(chapter_path, chapter)
        # A chapter below its minimum is only extended with new paragraphs
        # appended to the saved text; it is never regenerated or replaced.
        for _attempt in range(CHAPTER_EXPANSION_ATTEMPTS):
            if word_count(chapter) >= preferred_words:
                break
            missing = preferred_words - word_count(chapter)
            # Small deficits were repeatedly causing permanent production failure
            # because the model often returned less than a 100-word continuation.
            # Ask for a useful minimum continuation while still staying within the
            # section's target + 150-word envelope for the deficits we permit here.
            request_words = max(350, missing + 300)
            continuation_prompt = f"""التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

أكمل القسم التالي من قصة تاريخية للنوم بفقرات جديدة تأتي بعد نهايته مباشرة، بنحو {request_words} كلمة عربية.
أكمل من نهاية النص مباشرة؛ لا تلخص ولا تكرر ما سبق ولا تضف عنوانًا ولا خاتمة ولا شكرًا. أضف فقط تفاصيل يدعمها المصدر المعرفي، دون حشو ودون أسماء أو تواريخ لمجرد زيادة الكلمات. حافظ على المرحلة {section.get('phase', '')} وعلى الشخصية والمهمة نفسهما ونبرة هادئة بالغة، دون فتح أسئلة جديدة كبيرة ودون تقديم الشخصية الخيالية كحقيقة تاريخية.

الموضوع: {topic['topic_ar']}
القسم: {section.get('title', '')} — {section.get('brief', '')}
المشاهد: {json.dumps(section.get('scenes', []), ensure_ascii=False)}
المصدر المعرفي:
{research[:36000]}

نص القسم حتى الآن:
{chapter}
"""
            continuation, _ = gemini(continuation_prompt, max_tokens=4000, temperature=0.4)
            if word_count(continuation) > 0:
                chapter = f"{chapter.rstrip()}\n\n{continuation.strip()}"
                write(chapter_path, chapter)
        if word_count(chapter) < preferred_words:
            print(
                f"CHAPTER_LENGTH_SOFT_WARNING: chapter {index} has {word_count(chapter)} words; "
                f"preferred target is {preferred_words}. Continuing because per-chapter length is a soft target; "
                "the assembled story still has a hard 5000-word gate.",
                file=sys.stderr,
                flush=True,
            )
        chapters.append(chapter)
    return "\n\n".join(chapters)


def ensure_minimum_story_length(
    topic: dict,
    research: str,
    sections: list[dict],
    logs: Path,
    draft: str,
    *,
    minimum_words: int = MIN_STORY_WORDS,
    target_words: int = STORY_TOPUP_TARGET_WORDS,
    max_attempts: int = STORY_TOPUP_MAX_ATTEMPTS,
) -> str:
    """Append only until the assembled story clears the hard minimum.

    New episodes and resumed episodes use the same repair path. Repairs rotate
    backwards through non-final chapters so the final SOFT_FADE stays untouched
    whenever possible. Every successful append is written immediately, and in
    production it is also persisted as a durable checkpoint before another model
    call can happen.
    """
    current = word_count(draft)
    if current >= minimum_words:
        return draft
    if not sections:
        return draft

    safety_target = max(minimum_words, target_words)
    non_final = [
        index
        for index in range(len(sections) - 1, 0, -1)
        if (logs / f"chapter_{index:02d}.md").exists()
    ]
    if non_final:
        target_indexes = non_final
    else:
        target_indexes = [
            index
            for index in range(len(sections), 0, -1)
            if (logs / f"chapter_{index:02d}.md").exists()
        ]
    if not target_indexes:
        return draft

    repaired = draft
    for attempt in range(1, max_attempts + 1):
        current = word_count(repaired)
        if current >= minimum_words:
            break

        target_index = target_indexes[(attempt - 1) % len(target_indexes)]
        target_path = logs / f"chapter_{target_index:02d}.md"
        section = sections[target_index - 1]
        chapter = target_path.read_text(encoding="utf-8").strip()
        missing_to_target = max(1, safety_target - current)
        request_words = min(
            STORY_TOPUP_MAX_REQUEST_WORDS,
            max(450, missing_to_target + 250),
        )
        prompt = f"""أكمل هذا القسم من قصة تاريخية للنوم بإضافة جديدة فقط، بنحو {request_words} كلمة عربية.

هذه محاولة إصلاح طول رقم {attempt} من {max_attempts}. القصة المجمعة حاليًا {current} كلمة عربية،
والحد الأدنى الصارم {minimum_words} كلمة، والهدف الآمن {safety_target} كلمة.
قواعد صارمة:
- أكمل من نهاية القسم مباشرة ولا تعِد كتابة أي جزء موجود.
- لا تلخص ولا تكرر ولا تضف عنوانًا أو خاتمة جديدة.
- لا تفتح لغزًا أو سؤالًا جديدًا ولا ترفع الإثارة.
- حافظ على الشخصية والمهمة والمرحلة {section.get('phase', '')}.
- أضف فقط تفاصيل يدعمها المصدر المعرفي؛ لا تخترع أسماء أو تواريخ أو حقائق.
- اجعل الإضافة هادئة وطبيعية ومن دون حشو.
- لا تُنهِ القصة هنا إن لم يكن هذا هو القسم الأخير؛ أضف امتدادًا طبيعيًا داخل هذا القسم فقط.

الموضوع: {topic.get('topic_ar', '')}
القسم: {section.get('title', '')} — {section.get('brief', '')}
المشاهد: {json.dumps(section.get('scenes', []), ensure_ascii=False)}

المصدر المعرفي:
{research[:36000]}

نص القسم حتى الآن:
{chapter}
"""
        continuation, _ = gemini(prompt, max_tokens=6000, temperature=0.35)
        added_words = word_count(continuation)
        if added_words <= 0:
            print(
                f"STORY_LENGTH_TOPUP_EMPTY: attempt={attempt} chapter={target_index} current={current}",
                file=sys.stderr,
                flush=True,
            )
            continue

        write(target_path, f"{chapter.rstrip()}\n\n{continuation.strip()}\n")
        if os.environ.get("SLEEP_HISTORY_DURABLE_CHECKPOINTS") == "1":
            durable_checkpoint(logs.parent, f"story_length_topup_{attempt}", "chapters")

        rebuilt: list[str] = []
        for index in range(1, len(sections) + 1):
            path = logs / f"chapter_{index:02d}.md"
            if path.exists():
                rebuilt.append(path.read_text(encoding="utf-8").strip())
        repaired = "\n\n".join(part for part in rebuilt if part) or repaired
        new_count = word_count(repaired)
        print(
            f"STORY_LENGTH_LOCAL_TOPUP: attempt={attempt} chapter={target_index} "
            f"{current} -> {new_count} Arabic words (+{new_count - current})",
            file=sys.stderr,
            flush=True,
        )

    final_count = word_count(repaired)
    if final_count >= minimum_words:
        print(
            f"STORY_LENGTH_REPAIR_COMPLETE: {final_count} Arabic words "
            f"(minimum={minimum_words}, safety_target={safety_target})",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"STORY_LENGTH_REPAIR_EXHAUSTED: {final_count} Arabic words after "
            f"{max_attempts} append-only attempts",
            file=sys.stderr,
            flush=True,
        )
    return repaired

def resume_partial_story(story: Path) -> Path:
    _remember_story(story)
    if (story / "logs/topic.json").exists():
        run_summary.update(checkpoint_saved=True)  # the saved topic is already a resumable checkpoint
    topic_path = story / "logs/topic.json"
    outline_path = story / "logs/outline.json"
    if not topic_path.exists():
        raise RuntimeError("Partial story is missing logs/topic.json")
    topic = json.loads(topic_path.read_text(encoding="utf-8"))
    research_path = story / "logs/research_notes_draft.md"
    sources_path = story / "logs/research_grounding_sources.json"
    if research_path.exists():
        research = research_path.read_text(encoding="utf-8")
        sources = json.loads(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else []
        run_summary.reused("research")
    else:
        run_summary.stage("research")
        research, sources = research_topic(topic, story)
        write(research_path, research)
        write(sources_path, json.dumps(sources, ensure_ascii=False, indent=2))
    if not (story / "logs/final_story_after_gates.md").exists():  # a finished story is past this gate
        topic, research, sources = ensure_research_alignment(
            story, topic, research, sources, ROOT / "config/topics_history.json")
    if outline_path.exists():
        sections = json.loads(outline_path.read_text(encoding="utf-8"))["sections"]
        run_summary.reused("outline")
    else:
        run_summary.stage("outline")
        sections = make_outline(topic, research)
        write(outline_path, json.dumps({"sections": sections}, ensure_ascii=False, indent=2))
        durable_checkpoint(story, "outline", "chapters")
    run_summary.stage("chapters")
    draft = draft_story(topic, research, sections, story / "logs")
    draft = ensure_minimum_story_length(topic, research, sections, story / "logs", draft)
    if word_count(draft) < MIN_STORY_WORDS:
        raise RuntimeError(
            f"STORY_LENGTH_REPAIR_EXHAUSTED: {word_count(draft)} Arabic words remain after "
            f"{STORY_TOPUP_MAX_ATTEMPTS} append-only repair attempts"
        )
    write(story / "logs/assembled_story.md", draft)
    durable_checkpoint(story, "chapters", "quality_gates")
    run_summary.stage("quality_gates")
    story_text, quality_gate, fact_check = finalize_story_text(story, topic, research, draft)
    durable_checkpoint(story, "quality_gates", "packaging")
    build_metadata_files(story, topic, research, sources, story_text, quality_gate, fact_check)
    history_path = ROOT / "config/topics_history.json"
    from state_machine import State
    upsert_history_checkpoint(history_path, story, topic, State.GENERATING)
    print(json.dumps({"ok": True, "story_path": str(story), "slug": topic["slug"]}, ensure_ascii=False))
    return story


def recent_published_packaging(limit: int = 10) -> list[dict]:
    if not ARCHIVE_INDEX_PATH.exists():
        return []
    records = json.loads(ARCHIVE_INDEX_PATH.read_text(encoding="utf-8"))
    published = [item for item in records if str(item.get("status", "")).lower() == "published"]
    recent = []
    for source in reversed(published[-limit:]):
        item = dict(source)
        if not item.get("title_suffix"):
            title = str(item.get("final_youtube_title") or item.get("title") or "")
            item["title_suffix"] = next((suffix for suffix in TITLE_SUFFIXES if suffix in title), "")
        recent.append(item)
    return recent


CLOUDFLARE_PROMPT_MAX_CHARS = 2048
THUMBNAIL_PROMPT_MAX_CHARS = 1600


PACKAGING_FIELDS = (
    "episode_title_core", "title_suffix", "final_youtube_title", "thumbnail_text", "thumbnail_prompt",
)
# Only these need language work; the suffix and the joined title are produced by code.
LLM_PACKAGING_FIELDS = ("episode_title_core", "thumbnail_text", "thumbnail_prompt")
FINAL_TITLE_MAX_CHARS = 95
MAX_PACKAGING_ATTEMPTS = 3
TRAVEL_MARKERS = ("قافلة", "قوافل", "طريق", "رحلة", "ميناء", "سفر", "تجارة", "ممر")


def choose_title_suffix(topic: dict, recent: list[dict], rejected: tuple[str, ...] | list[str] = ()) -> str:
    """Pick the title suffix deterministically (no model call).

    Never the previous three suffixes when another fits, balanced across the last
    ten, matched to the episode (journey suffixes for route/travel topics), with a
    stable hash as the final tie-break so the same inputs always give the same pick.
    """
    recent_suffixes = [str(item.get("title_suffix", "")).strip() for item in recent]
    pool = [suffix for suffix in TITLE_SUFFIXES if suffix not in rejected] or list(TITLE_SUFFIXES)
    fresh = [suffix for suffix in pool if suffix not in set(recent_suffixes[:3])] or pool
    usage = {suffix: sum(1 for used in recent_suffixes[:10] if used == suffix) for suffix in fresh}
    least = min(usage.values())
    balanced = [suffix for suffix in fresh if usage[suffix] == least]
    text = " ".join(str(topic.get(key, "")) for key in ("topic_ar", "angle_ar", "family"))
    travel = any(marker in text for marker in TRAVEL_MARKERS)
    key = str(topic.get("slug") or topic.get("topic_ar") or "")

    def rank(suffix: str) -> tuple[int, str]:
        matches = ("رحلة" in suffix) == travel
        return (0 if matches else 1, hashlib.sha256(f"{key}|{suffix}".encode("utf-8")).hexdigest())

    return sorted(balanced, key=rank)[0]


_NEGATION = re.compile(r"\b(?:no|not|never|without|avoid|don't|do not|zero)\b")
_POSITIVE_TEXT_INSTRUCTIONS = (
    re.compile(
        r"\b(?:include|add|write|render|draw|display|show|print|place|put|insert|overlay|with|in)\s+"
        r"(?:the\s+|an?\s+)?(?:exact\s+|large\s+|simple\s+|elegant\s+|arabic\s+|thumbnail\s+)*"
        r"(?:text|title|label|words?|letters?|typography|lettering|caption|phrase|inscription)\b"
    ),
    re.compile(r"\b(?:typography|lettering|caption|text|label|title)\s+(?:reading|saying|that\s+(?:reads|says)|which\s+(?:reads|says))\b"),
    re.compile(r"\b(?:reading|saying|text\s+reads|that\s+(?:reads|says)|the\s+words?)\s*[:\"'“«]*\s*[ء-ي]"),
    re.compile(r"\b(?:large|simple|elegant|bold|ivory|off-white)\b[^.;]{0,40}\btypography\b"),
)


def thumbnail_prompt_text_conflicts(prompt: str, label: str) -> list[str]:
    """Reasons a thumbnail prompt asks the image model to draw text (empty = background-only)."""
    reasons: list[str] = []
    if label and label in prompt:
        reasons.append("contains the exact thumbnail_text")
    if re.search(r"[ء-ي]", prompt):
        reasons.append("contains Arabic text")
    lowered = prompt.lower()
    for pattern in _POSITIVE_TEXT_INSTRUCTIONS:
        for match in pattern.finditer(lowered):
            clause_start = max(lowered.rfind(mark, 0, match.start()) for mark in ".;!?\n") + 1
            if _NEGATION.search(lowered[clause_start:match.start()]):
                continue  # e.g. "Do not render letters" is the allowed negative form
            reasons.append(f"asks the image model to render text ('{match.group(0).strip()}')")
            break
    return reasons


def packaging_field_issues(package: dict, topic: dict, recent: list[dict]) -> dict[str, str]:
    """Deterministic content checks: field -> exact reason. Empty dict means valid."""
    issues: dict[str, str] = {}
    for field in PACKAGING_FIELDS:
        if not str(package.get(field, "")).strip():
            issues[field] = "missing"
    if issues:
        return issues
    core = str(package["episode_title_core"]).strip()
    suffix = str(package["title_suffix"]).strip()
    final = str(package["final_youtube_title"]).strip()
    label = str(package["thumbnail_text"]).strip()
    prompt = str(package["thumbnail_prompt"]).strip()
    core_words = re.findall(r"[ء-ي]+", core)
    label_words = re.findall(r"[ء-ي]+", label)
    if not 3 <= len(core_words) <= 10:
        issues["episode_title_core"] = f"has {len(core_words)} Arabic words; it must be 4-9 Arabic words"
    if suffix not in TITLE_SUFFIXES:
        issues["title_suffix"] = "is not one of the approved suffixes"
    if final != f"{core} | {suffix}":
        issues["final_youtube_title"] = "must be exactly '[core] | [suffix]'"
    elif len(final) > FINAL_TITLE_MAX_CHARS:
        issues["final_youtube_title"] = f"is {len(final)} characters; the maximum is {FINAL_TITLE_MAX_CHARS}"
    elif final.count("😴") != 1:
        issues["final_youtube_title"] = "must contain exactly one sleep emoji (the core must not add one)"
    recent_suffixes = [str(item.get("title_suffix", "")) for item in recent[:3]]
    if suffix and suffix in recent_suffixes:
        issues["title_suffix"] = "repeats one of the previous three suffixes"
    forbidden_labels = {"قصة للنوم", "قصة تاريخية", "حكاية للنوم", "للنوم", "قبل النوم"}
    recent_labels = {str(item.get("thumbnail_text", "")).strip() for item in recent}
    if not 1 <= len(label_words) <= 4:
        issues["thumbnail_text"] = f"has {len(label_words)} Arabic words; it must be 2-4 Arabic words"
    elif label in forbidden_labels or "😴" in label:
        issues["thumbnail_text"] = "is a generic label or contains an emoji"
    elif label in recent_labels:
        issues["thumbnail_text"] = "repeats a recent thumbnail label"
    if len(prompt) > THUMBNAIL_PROMPT_MAX_CHARS:
        issues["thumbnail_prompt"] = f"is {len(prompt)} characters; the maximum is {THUMBNAIL_PROMPT_MAX_CHARS}"
    forbidden_prompt_phrases = {"[topic_ar]", "[thumbnail_text]", "${", "{{", "}}"}
    if any(forbidden in prompt for forbidden in forbidden_prompt_phrases):
        issues["thumbnail_prompt"] = "contains an unfilled placeholder"
    lowered = prompt.lower()
    has_no_logo_watermark = (
        ("no logo" in lowered or "no watermark" in lowered or "without logo" in lowered or "without watermark" in lowered)
        or ("logo" not in lowered and "watermark" not in lowered)
    )
    has_text_restriction = (
        "no text" in lowered or "no word" in lowered or "no generated" in lowered or "no typography" in lowered
        or "background only" in lowered or "without text" in lowered or "no overlay" in lowered
    )
    missing_parts = [
        name for name, ok in (
            ("'16:9 YouTube thumbnail'", "16:9 YouTube thumbnail" in prompt),
            ("a background/scene description", "background" in lowered or "scene" in lowered),
            ("an explicit no-text instruction", has_text_restriction),
            ("a no-logo/no-watermark instruction", has_no_logo_watermark),
        ) if not ok
    ]
    if missing_parts and "thumbnail_prompt" not in issues:
        issues["thumbnail_prompt"] = "is missing " + ", ".join(missing_parts)
    conflicts = thumbnail_prompt_text_conflicts(prompt, label)
    if conflicts:
        # A positive text request is invalid even when "no text" also appears:
        # the Arabic label is drawn later by the deterministic renderer.
        issues["thumbnail_prompt"] = (
            "must be a background-only prompt with a natural uncluttered text-safe area; it " + "; ".join(conflicts)
        )
    return issues


def local_packaging_issues(package: dict, topic: dict, recent: list[dict]) -> list[str]:
    return sorted(packaging_field_issues(package, topic, recent))


def _read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _with_joined_title(package: dict) -> dict:
    core = str(package.get("episode_title_core", "")).strip()
    suffix = str(package.get("title_suffix", "")).strip()
    if core and suffix:
        package["final_youtube_title"] = f"{core} | {suffix}"
    return package


def generate_youtube_packaging(story: Path, topic: dict) -> dict:
    log_path = story / "logs" / "youtube_packaging.json"
    attempt_log_path = story / "logs" / "youtube_packaging_attempts.json"
    partial_path = story / "logs" / "youtube_packaging_partial.json"
    topic_hash = hashlib.sha256(json.dumps(topic, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    recent = recent_published_packaging(10)
    recent_hash = hashlib.sha256(json.dumps(recent, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    rules = YOUTUBE_PACKAGING_RULES_PATH.read_text(encoding="utf-8")
    rules_hash = hashlib.sha256(rules.encode("utf-8")).hexdigest()
    if log_path.exists():
        cached = json.loads(log_path.read_text(encoding="utf-8"))
        if (
            cached.get("status") == "PASS"
            and cached.get("topic_sha256") == topic_hash
            and cached.get("recent_packaging_sha256") == recent_hash
            and cached.get("rules_sha256") == rules_hash
        ):
            return cached["package"]
    run_summary.stage("packaging")
    setting = f"{topic.get('location_ar', '')} — {topic.get('period_ar', '')}".strip(" —")
    inputs = {"topic_sha256": topic_hash, "recent_packaging_sha256": recent_hash, "rules_sha256": rules_hash}
    partial = _read_json(partial_path, {})
    resumed = isinstance(partial, dict) and partial.get("inputs") == inputs
    package: dict = dict(partial.get("package") or {}) if resumed else {}
    rejected_suffixes: list[str] = list(partial.get("rejected_suffixes") or []) if resumed else []
    package["title_suffix"] = choose_title_suffix(topic, recent, rejected_suffixes)
    _with_joined_title(package)

    def checkpoint() -> None:
        write(partial_path, json.dumps({
            "inputs": inputs, "package": package, "rejected_suffixes": rejected_suffixes,
            "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2))
        run_summary.update(checkpoint_saved=True, resume_stage="packaging")

    checkpoint()  # progress is on disk before the first provider call
    details: dict[str, str] = packaging_field_issues(package, topic, recent)
    for attempt in range(MAX_PACKAGING_ATTEMPTS):
        details = packaging_field_issues(package, topic, recent)
        # The suffix and joined title are code-owned; a bad joined title is fixed through the core.
        ask = [f for f in LLM_PACKAGING_FIELDS if f in details]
        if ("final_youtube_title" in details or "title_suffix" in details) and "episode_title_core" not in ask:
            ask.append("episode_title_core")
        if ask:
            reasons = {f: details.get(f) or details.get("final_youtube_title") or "invalid" for f in ask}
            keep = {k: v for k, v in package.items() if k not in ask}
            prompt = f"""طبّق قواعد حزمة YouTube التالية حرفيًا.
{rules}

EPISODE TOPIC: {topic['topic_ar']}
HISTORICAL SETTING: {setting}
ANGLE: {topic.get('angle_ar', '')}
RECENT TEN PUBLISHED RECORDS:
{json.dumps(recent, ensure_ascii=False)}

لاحقة العنوان تُختار آليًا وهي: {package['title_suffix']}
العنوان النهائي سيكون: [episode_title_core] | {package['title_suffix']} ولا يجوز أن يتجاوز {FINAL_TITLE_MAX_CHARS} حرفًا.
أعد توليد الحقول التالية فقط مع سبب كل رفض:
{json.dumps(reasons, ensure_ascii=False)}
حافظ على القيم الصالحة التالية كما هي ولا تُرجعها:
{json.dumps(keep, ensure_ascii=False)}

Return JSON only with exactly these keys: {json.dumps(ask)}
"""
            text, _ = gemini(prompt, json_mode=True, max_tokens=3000, temperature=0.35)
            try:
                candidate = parse_json_object(text, "YOUTUBE_PACKAGING_SCHEMA_FAILED")
            except (ValueError, KeyError):
                candidate = {}
            for field in ask:
                value = str(candidate.get(field, "")).strip() if isinstance(candidate, dict) else ""
                if value:
                    package[field] = value
            _with_joined_title(package)
            checkpoint()
            details = packaging_field_issues(package, topic, recent)
        if details:
            diagnostics = {
                "attempt": attempt + 1,
                "deterministic_failures": sorted(details),
                "reasons": details,
                "semantic_validation": [],
            }
            if len(package.get("thumbnail_prompt", "")) > THUMBNAIL_PROMPT_MAX_CHARS:
                diagnostics["prompt_length_error"] = f"prompt length {len(package.get('thumbnail_prompt', ''))} exceeds {THUMBNAIL_PROMPT_MAX_CHARS}"
            _append_attempt(attempt_log_path, diagnostics)
            continue
        validation_prompt = f"""أنت بوابة مستقلة لحزمة YouTube. لا تعيد الكتابة.
تحقق من الدقة التاريخية، طبيعة العربية، ظهور الموضوع أولًا، نص مصغر من كلمتين إلى أربع، تكامل النص مع العنوان، مشهد واحد خاص بالحلقة، هوية الألوان، وضوح الهاتف، غياب التضليل والوجوه العملاقة والـclickbait والفانتازيا والمظهر السياحي اللامع.
لاحقة العنوان مختارة آليًا ولا تُقيَّم.
الموضوع: {json.dumps(topic, ensure_ascii=False)}
آخر عشر حلقات: {json.dumps(recent, ensure_ascii=False)}
الحزمة: {json.dumps(package, ensure_ascii=False)}
أعد JSON فقط: {{"status":"PASS أو FAIL","title":true,"thumbnail_text":true,"thumbnail_prompt":true,"failing_fields":[],"issues":[]}}
"""
        validation_text, _ = gemini(validation_prompt, json_mode=True, max_tokens=1600, temperature=0.0)
        try:
            validation = parse_json_object(validation_text, "YOUTUBE_PACKAGING_VALIDATION_SCHEMA_FAILED")
        except (ValueError, KeyError):
            validation = {"status": "FAIL", "failing_fields": list(LLM_PACKAGING_FIELDS), "issues": ["unparseable validation"]}
        if (
            validation.get("status") == "PASS"
            and validation.get("title") is True
            and validation.get("thumbnail_text") is True
            and validation.get("thumbnail_prompt") is True
            and not (validation.get("failing_fields") or [])
            and not (validation.get("issues") or [])
        ):
            write(log_path, json.dumps({
                "status": "PASS", **inputs,
                "historical_setting": setting, "recent_records_checked": len(recent),
                "package": package, "validation": validation,
            }, ensure_ascii=False, indent=2))
            return package
        failing = list(validation.get("failing_fields") or LLM_PACKAGING_FIELDS)
        for field in failing:
            if field in LLM_PACKAGING_FIELDS:
                package.pop(field, None)
            elif field in ("final_youtube_title", "episode_title_core"):
                package.pop("episode_title_core", None)
            elif field == "title_suffix":
                rejected_suffixes.append(package["title_suffix"])
                package["title_suffix"] = choose_title_suffix(topic, recent, rejected_suffixes)
        _with_joined_title(package)
        checkpoint()
        details = {f: "rejected by the semantic quality gate: " + "; ".join(map(str, validation.get("issues") or [])) for f in failing}
        _append_attempt(attempt_log_path, {
            "attempt": attempt + 1,
            "deterministic_failures": failing,
            "semantic_validation": validation.get("issues", []),
        })
    field = next(iter(details), "unknown")
    reason = details.get(field, "")
    raise RuntimeError(f"YOUTUBE_PACKAGING_VALIDATION_FAILED: {field}" + (f" ({reason})" if reason and reason != "missing" else ""))


def _append_attempt(path: Path, record: dict) -> None:
    if not path.parent.exists():
        return
    existing = _read_json(path, [])
    existing = existing if isinstance(existing, list) else []
    existing.append(record)
    write(path, json.dumps(existing, ensure_ascii=False, indent=2))


def build_metadata_files(story: Path, topic: dict, research: str, sources: list[dict], story_text: str,
                         quality_gate: dict, fact_check: dict) -> None:
    today = dt.date.today().isoformat()
    source_lines = ["# المصادر", "", f"تاريخ الوصول: {today}", ""]
    for index, source in enumerate(sources[:12], 1):
        source_lines += [f"## {index}. {source['title']}", "", f"- الرابط: {source['url']}", "- الاستخدام: التحقق من السياق التاريخي والآثاري الوارد في ملاحظات البحث.", ""]
    if not sources:
        source_lines += ["- راجع الروابط المباشرة الواردة داخل `04_research_notes.md` قبل الاعتماد."]

    # Story, quality gates and fact checks are finished: save that fact before the
    # (small) packaging stage so a provider outage here only ever resumes packaging.
    write(story / "logs" / "story_ready_for_packaging.json", json.dumps({
        "story_sha256": hashlib.sha256(story_text.encode("utf-8")).hexdigest(),
        "word_count": word_count(story_text),
        "next_stage": "packaging",
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))
    run_summary.update(checkpoint_saved=True, resume_stage="packaging")
    packaging = generate_youtube_packaging(story, topic)
    title = packaging["final_youtube_title"]

    description_template = (ROOT / "config" / "youtube_description_template.txt").read_text(encoding="utf-8")
    topic_block = f"""📖 قصة اليوم:
{topic['topic_ar']}

نأخذك هذه الليلة بهدوء إلى {topic['angle_ar']}.

المحتوى مبني على مصادر تاريخية وأثرية، مع توضيح المعلومات التي ما زالت محل نقاش بين الباحثين.

📚 المصادر:
راجع قائمة المصادر الكاملة المرفقة مع الحلقة."""
    description = description_template.replace("{{TOPIC_BLOCK}}", topic_block).strip() + "\n"
    # The pipeline's prepare stage performs validated full Arabic diacritization
    # and inserts human-readable/Fish direction tags before audio generation.
    annotated = story_text
    api_text = story_text
    thumbnail_prompt = packaging["thumbnail_prompt"]
    if thumbnail_prompt_text_conflicts(thumbnail_prompt, str(packaging.get("thumbnail_text", "")).strip()):
        raise RuntimeError("Thumbnail prompt must be background-only: it asks the image model to render text")
    image_prompt = thumbnail_prompt + """

PIPELINE SCENE RENDER: Generate the historical scene with a natural uncluttered text-safe area integrated into the scene only. Do not render letters, words, typography, logos, or watermarks; the exact Arabic label will be overlaid deterministically after generation. Do not create a panel, box, card, banner, frame, solid dark patch or artificial rectangle."""

    write(story / "01_title.txt", title)
    write(story / "02_description.txt", description)
    write(story / "03_sources.md", "\n".join(source_lines))
    write(story / "04_research_notes.md", research)
    write(story / "05_story_clean.md", story_text)
    write(story / "06_story_tts_annotated.md", annotated)
    write(story / "07_story_tts_api.md", api_text)
    write(story / "08_image_prompt_en.txt", image_prompt)
    write(story / "08_thumbnail_prompt_en.txt", thumbnail_prompt)
    write(story / "09_image_source.md", "# مصدر الصورة\n\nImage generation pending")
    write(story / "16_fact_check.md", "# Fact Check\n\n```json\n" + json.dumps(fact_check, ensure_ascii=False, indent=2) + "\n```")
    from state_machine import State
    write(story / "17_status.md", f"# الحالة\n\n- الحالة الحالية: `{State.GENERATING}`\n- النشر: `not_published`.\n- سيحدّث خط الإنتاج هذه الحالة بعد اكتمال الصوت والفيديو والتحقق.")
    metadata = {
        "title": title,
        "episode_title_core": packaging["episode_title_core"],
        "title_suffix": packaging["title_suffix"],
        "final_youtube_title": title,
        "thumbnail_text": packaging["thumbnail_text"],
        "thumbnail_prompt": thumbnail_prompt,
        "story_format": topic.get("story_format", ""),
        "narrative_identity": topic.get("narrative_identity", quest_rules.IDENTITY),
        **{key: topic[key] for key in (*quest_rules.PREMISE_FIELDS, "penalty_flags") if topic.get(key)},
        "thumbnail_scene_type": topic.get("family") or "historical-place-immersion",
        "historical_setting": f"{topic.get('location_ar', '')} — {topic.get('period_ar', '')}".strip(" —"),
        "youtube_packaging_status": "passed",
        "youtube_packaging_validation_status": "passed",
        "topic": topic["topic_ar"],
        "slug": topic["slug"],
        "created_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=4))).isoformat(),
        "word_count": word_count(story_text),
        "audio_duration_seconds": 0,
        "video_duration_seconds": 0,
        "voice_provider": SETTINGS.get("tts_provider", "edge"),
        "voice_name": SETTINGS.get("tts_voice", "ar-AE-FatimaNeural"),
        "voice_id": "",
        "tts_model": "Microsoft Edge neural TTS free fallback",
        "research_sources_count": len(sources),
        "topic_gate_status": "passed",
        **{key: topic.get(key) for key in research_alignment.METADATA_KEYS},
        "grounded_research_status": "passed",
        "source_validation_status": "passed",
        "language_gate_status": "passed",
        "religion_gate_status": "passed",
        "sleep_gate_status": "passed",
        "fact_check_status": "passed",
        "fact_corrections_applied": True,
        "second_fact_check_status": "passed",
        "quality_gate": quality_gate,
        "image_provider": None,
        "image_model": None,
        "image_generation_method": None,
        "image_generated_at": None,
        "image_path": "",
        "image_generation_status": "pending",
        "audio_path": "",
        "video_path": "",
        "thumbnail_path": "",
        "approval_status": State.GENERATING,
        "publish_status": "not_published",
        "youtube_video_id": None,
    }
    write(story / "15_metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))


def durable_checkpoint(story: Path, completed: str, next_stage: str | None) -> None:
    """Record a real checkpoint in the run summary and copy its small text state to automation-state.

    Best effort: a persistence problem is a warning, never a production failure.
    """
    relative = story.relative_to(ROOT).as_posix() if story.is_relative_to(ROOT) else str(story)
    run_summary.mark_checkpoint(relative, completed, next_stage)
    if os.environ.get("SLEEP_HISTORY_DURABLE_CHECKPOINTS") == "1":
        try:
            state_store.save_story_checkpoint(story)
        except Exception as exc:
            print(f"Warning: durable checkpoint not saved: {exc}", file=sys.stderr, flush=True)


ALIGNMENT_DOWNSTREAM = ("outline.json", "assembled_story.md", "content_gates.json", "final_story_after_gates.md")


def ensure_research_alignment(
    story: Path, topic: dict, research: str, sources: list[dict], history_path: Path,
) -> tuple[dict, str, list[dict]]:
    """Hard gate between research and outline: the sources must support the episode's angle."""
    run_summary.stage("research_alignment")
    logs = story / "logs"
    extra_sources: list[dict] = []

    def search_more(adjusted: dict) -> str:
        try:
            extra, found = research_topic(adjusted, story)
        except llm_router.LlmQuotaWaiting:
            raise
        except RuntimeError as error:
            if not str(error).startswith(("SOURCE_VALIDATION_FAILED", "GROUNDED_RESEARCH_UNAVAILABLE")):
                raise
            return ""  # no extra evidence found: the gate decides on what it has
        extra_sources.extend(found)
        return extra

    before = research_alignment.topic_view(topic)
    new_topic, new_research, record = research_alignment.run_gate(
        gemini, parse_json, topic, research, cache_path=logs / "research_topic_alignment.json",
        search_more=search_more,
    )
    if new_research != research:
        sources = sources + [item for item in extra_sources if item not in sources]
        write(logs / "research_notes_draft.md", new_research)
        write(logs / "research_grounding_sources.json", json.dumps(sources, ensure_ascii=False, indent=2))
    if research_alignment.topic_view(new_topic) != before:
        # The angle changed: an outline or chapters written for the old angle must not survive.
        for name in ALIGNMENT_DOWNSTREAM:
            (logs / name).unlink(missing_ok=True)
        for chapter in logs.glob("chapter_*.md"):
            chapter.unlink(missing_ok=True)
        print(f"RESEARCH_TOPIC_ALIGNMENT=REANCHORED elements={record['unsupported_topic_elements']}", file=sys.stderr, flush=True)
    write(logs / "topic.json", json.dumps(new_topic, ensure_ascii=False, indent=2))
    if history_path.exists():
        from state_machine import State
        upsert_history_checkpoint(history_path, story, new_topic, State.GENERATING)
    durable_checkpoint(story, "research_alignment", "outline")
    return new_topic, new_research, sources


def create_daily_story(dry_run: bool = False) -> Path | None:
    history_path = ROOT / "config/topics_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    if not dry_run:
        migrated = episode_selection.migrate_zombie_records(history, ROOT)
        if migrated:
            write(history_path, json.dumps(history, ensure_ascii=False, indent=2))
            print(f"Retired zombie history record(s) as ABANDONED_LEGACY: {migrated}", file=sys.stderr, flush=True)

        def checkpoint_path(item: dict) -> Path | None:
            return episode_selection.story_dir(item, ROOT)

        try:
            state_episodes = state_store._load_state().get("episodes", {})
        except Exception:
            state_episodes = {}
        # One canonical rule shared with the checkpoint restore step: an unfinished
        # episode (GENERATING, FAILED_RETRYABLE, WAITING_FOR_LLM_QUOTA, upload not
        # finished) always wins, so a new topic is never started while one exists.
        unfinished = episode_selection.active_episode(history, ROOT, state_episodes)
        if unfinished:
            print(
                f"RESUMING active episode {unfinished.get('slug')} ({unfinished.get('status')}); "
                "no new topic will be selected.",
                file=sys.stderr, flush=True,
            )
        if unfinished:
            saved_story = checkpoint_path(unfinished).resolve()
            if saved_story.exists():
                if unfinished.get("status") in {"READY_FOR_APPROVAL", "MEDIA_READY"} and (saved_story / "15_metadata.json").exists():
                    print(json.dumps({"ok": True, "story_path": str(saved_story), "slug": unfinished["slug"]}, ensure_ascii=False))
                    return saved_story
                return resume_partial_story(saved_story)
            topic = topic_from_checkpoint(unfinished)
            story = saved_story
        else:
            run_summary.stage("topic_selection")
            topic = choose_valid_topic(history)
            story = ROOT / "stories" / f"{dt.date.today().isoformat()}_{topic['slug']}"
    else:
        run_summary.stage("topic_selection")
        topic = choose_valid_topic(history)
        story = ROOT / "stories" / f"{dt.date.today().isoformat()}_{topic['slug']}"
    if dry_run:
        print(json.dumps({"ok": True, "mode": "dry-run", "topic": topic}, ensure_ascii=False))
        return None
    if story.exists():
        raise RuntimeError(f"Story directory already exists: {story}")
    story.mkdir(parents=True)
    (story / "logs").mkdir()
    _remember_story(story)
    write(story / "logs/topic.json", json.dumps(topic, ensure_ascii=False, indent=2))
    # Register the selected topic before the first long research request. If a
    # provider call fails later, the next run resumes this exact directory and
    # its saved chapters instead of selecting a different topic.
    from state_machine import State
    upsert_history_checkpoint(history_path, story, topic, State.GENERATING)
    durable_checkpoint(story, "topic_selection", "research")
    run_summary.stage("research")
    research, sources = research_topic(topic, story)
    write(story / "logs/research_notes_draft.md", research)
    write(story / "logs/research_grounding_sources.json", json.dumps(sources, ensure_ascii=False, indent=2))
    topic, research, sources = ensure_research_alignment(story, topic, research, sources, history_path)
    run_summary.stage("outline")
    outline = make_outline(topic, research)
    write(story / "logs/outline.json", json.dumps({"sections": outline}, ensure_ascii=False, indent=2))
    durable_checkpoint(story, "outline", "chapters")
    run_summary.stage("chapters")
    draft = draft_story(topic, research, outline, story / "logs")
    draft = ensure_minimum_story_length(topic, research, outline, story / "logs", draft)
    if word_count(draft) < MIN_STORY_WORDS:
        raise RuntimeError(
            f"STORY_LENGTH_REPAIR_EXHAUSTED: {word_count(draft)} Arabic words remain after "
            f"{STORY_TOPUP_MAX_ATTEMPTS} append-only repair attempts"
        )
    write(story / "logs/assembled_story.md", draft)
    durable_checkpoint(story, "chapters", "quality_gates")
    run_summary.stage("quality_gates")
    story_text, quality_gate, fact_check = finalize_story_text(story, topic, research, draft)
    durable_checkpoint(story, "quality_gates", "packaging")
    build_metadata_files(story, topic, research, sources, story_text, quality_gate, fact_check)
    from state_machine import State
    upsert_history_checkpoint(history_path, story, topic, State.GENERATING)
    print(json.dumps({"ok": True, "story_path": str(story), "slug": topic["slug"]}, ensure_ascii=False))
    return story


EXIT_WAITING_FOR_QUOTA = 75


def _remember_story(story: Path) -> None:
    try:
        import episode_id
        episode = episode_id.extract_episode_id_from_path(story) or ""
    except Exception:
        episode = ""
    run_summary.update(story_path=story.relative_to(ROOT).as_posix() if story.is_relative_to(ROOT) else str(story),
                       episode_id=episode)


def mark_history_status(story_path: str, status: str, **fields: object) -> bool:
    """Set the status (and wait details) of the history record that owns ``story_path``."""
    from state_machine import transition_state
    history_path = ROOT / "config" / "topics_history.json"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    name = Path(story_path.replace("\\", "/")).name
    for record in history:
        if Path(str(record.get("story_path") or "").replace("\\", "/")).name == name:
            try:
                record["status"] = transition_state(record.get("status", ""), status)
            except ValueError:
                return False
            record.update(fields)
            write(history_path, json.dumps(history, ensure_ascii=False, indent=2))
            return True
    return False


def handle_quota_waiting(waiting: "llm_router.LlmQuotaWaiting") -> None:
    """Park the active episode: planned pause, nothing is lost, the next run resumes it."""
    from state_machine import State
    run_summary.fail(waiting)
    summary = run_summary.load()
    story_path = summary.get("story_path") or ""
    if story_path:
        mark_history_status(
            story_path, State.WAITING_FOR_LLM_QUOTA,
            waiting_for="llm_quota", waiting_stage=summary.get("current_stage"),
            blocked_until=waiting.blocked_until, quota_kind=waiting.kind,
            waiting_since=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        try:
            state_store.save_episode_state(Path(story_path).name, {
                "approval_status": State.WAITING_FOR_LLM_QUOTA, "story_path": story_path,
                "waiting_stage": summary.get("current_stage"), "blocked_until": waiting.blocked_until,
            })
        except Exception as exc:
            print(f"Warning: episode wait state not saved to automation-state: {exc}", file=sys.stderr)
    print(json.dumps({
        "ok": False, "status": State.WAITING_FOR_LLM_QUOTA, "story_path": story_path,
        "stage": summary.get("current_stage"), "blocked_until": waiting.blocked_until,
        "quota_kind": waiting.kind, "models_attempted": waiting.models_attempted,
    }, ensure_ascii=False), flush=True)


def main() -> None:
    state_store.ensure_hydrated()
    run_summary.start(workflow="generate_daily", action="generate")
    try:
        _main()
    except llm_router.LlmQuotaWaiting as waiting:
        handle_quota_waiting(waiting)
        raise SystemExit(EXIT_WAITING_FOR_QUOTA)
    except SystemExit:
        raise
    except BaseException as exc:
        run_summary.fail(exc)
        raise


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Choose a topic and verify Gemini without creating files")
    parser.add_argument("--resume-partial", type=Path, help="Resume a generated draft from its saved topic, research, outline, and chapters")
    args = parser.parse_args()
    if args.resume_partial:
        resume_partial_story(args.resume_partial.resolve())
    else:
        create_daily_story(args.dry_run)


if __name__ == "__main__":
    main()
