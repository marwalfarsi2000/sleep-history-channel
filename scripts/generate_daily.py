from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
MASTER_RULES_PATH = ROOT / "config" / "master_production_rules.md"
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
        "thinkingConfig": (
            {"thinkingLevel": "minimal"}
            if model.startswith("gemini-3") and model != "gemini-3.1-flash-lite"
            else {"thinkingBudget": 0}
        ),
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
    result = None
    last_error = ""
    retry_delays = GEMINI_RETRY_DELAYS
    for attempt in range(len(retry_delays) + 1):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            last_error = f"Gemini HTTP {error.code}: {body[:600]}"
            if error.code == 429 and _daily_quota_exhausted(body):
                raise RuntimeError(
                    "Gemini daily quota is exhausted; checkpoint preserved for the next run"
                ) from error
            if error.code not in (429, 500, 502, 503, 504) or attempt == len(retry_delays):
                prefix = "GROUNDED_RESEARCH_UNAVAILABLE: " if search else ""
                raise RuntimeError(prefix + last_error) from error
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = max(retry_delays[attempt], int(retry_after or 0))
            except ValueError:
                delay = retry_delays[attempt]
            print(
                f"Gemini temporary HTTP {error.code}; retrying in {delay}s "
                f"({attempt + 1}/{len(retry_delays)}): {_error_summary(body)}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
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
    last_error: RuntimeError | None = None
    models = _llm_models(search=search)
    for index, model in enumerate(models):
        try:
            return _gemini_one(
                model,
                prompt,
                json_mode=json_mode,
                search=search,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except RuntimeError as error:
            last_error = error
            message = str(error).lower()
            # 404/403 mean the model was retired or is not enabled for this
            # project; skip to the next approved model instead of failing.
            recoverable = any(
                marker in message
                for marker in ("http 429", "http 503", "http 404", "http 403", "daily quota", "unavailable")
            )
            if not recoverable or index + 1 >= len(models):
                raise
            print(
                f"Gemini writing model {model} is unavailable; switching to free fallback {models[index + 1]}",
                file=sys.stderr,
                flush=True,
            )
    raise last_error or RuntimeError("Gemini request failed")


def parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    return json.loads(cleaned)


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:70] or f"history-{dt.date.today():%Y%m%d}"


def word_count(text: str) -> int:
    return len(re.findall(r"[\u0600-\u06FF]+", text))


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
    history = json.loads(history_path.read_text(encoding="utf-8"))
    record = next((item for item in history if item.get("slug") == topic["slug"]), None)
    if record is None:
        record = {
            "slug": topic["slug"],
            "topic": topic["topic_ar"],
            "angle": topic["angle_ar"],
            "story_path": story.relative_to(ROOT).as_posix(),
            "created_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=4))).isoformat(),
        }
        history.append(record)
    record["status"] = status
    record["story_path"] = story.relative_to(ROOT).as_posix()
    # Keep enough provider input to rebuild the same episode when a hosted
    # runner disappears after persisting only the catalogue checkpoint.
    for key in ("topic_ar", "angle_ar", "search_query_en", "period_ar", "location_ar", "key_elements", "reason_ar"):
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


def choose_topic(history: list[dict]) -> dict:
    prior = "\n".join(
        f"- {item.get('topic', item.get('slug', ''))} | {item.get('angle', '')} | "
        f"{item.get('period_ar', '')} | {item.get('location_ar', '')} | {item.get('key_elements', [])}"
        for item in history
    ) or "لا يوجد سجل سابق"
    rules = production_rules(*range(2, 17))
    prompt = f"""أنت محرر قناة عربية اسمها «لن تُكمل هذه القصة» تقدم سردًا تاريخيًا حقيقيًا هادئًا للنوم.
التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

اختر موضوع الحلقة اليومية التالية. يجب أن تتوفر له مصادر متحفية أو جامعية أو أثرية كثيرة، وأن يسمح بوصف الحياة اليومية والمكان لمدة ساعة، وألا يكرر موضوعًا أو زاوية في السجل.

السجل السابق:
{prior}

أعد JSON فقط بهذه المفاتيح الإنجليزية:
topic_ar: اسم موضوع واضح بالعربية
angle_ar: زاوية الحلقة
slug: حروف إنجليزية صغيرة وشرطات فقط
search_query_en: استعلام بحث إنجليزي دقيق
period_ar: الفترة التاريخية
location_ar: المكان أو النطاق الجغرافي
key_elements: قائمة بالأحداث والنقاط والحياة اليومية الرئيسية التي ستغطيها الحلقة
reason_ar: سبب الاختيار في جملة
"""
    text, _ = gemini(prompt, json_mode=True, max_tokens=1200, temperature=0.7)
    topic = parse_json(text)
    topic["slug"] = safe_slug(str(topic.get("slug", "")))
    required = ("topic_ar", "angle_ar", "slug", "search_query_en", "period_ar", "location_ar", "reason_ar")
    missing = [key for key in required if not str(topic.get(key, "")).strip()]
    if missing:
        raise RuntimeError(f"Topic selection is missing required fields: {missing}")
    validation_prompt = f"""{production_rules(*range(2, 17))}

أنت بوابة مستقلة لاختيار الموضوع. قيّم المرشح مقارنة بالسجل السابق، ولا تحاول إصلاحه.
المرشح: {json.dumps(topic, ensure_ascii=False)}
السجل: {prior}

أعد JSON فقط بالشكل:
{{"status":"PASS أو REJECT","language":true,"religion":true,"sleep":true,"quality":true,"universality":true,"non_duplicate":true,"reasons":["..."]}}
كل قيمة true تعني أن البوابة نجحت. ارفض عند الشك الجوهري أو نقص المادة والمصادر.
"""
    validation_text, _ = gemini(
        validation_prompt, json_mode=True, max_tokens=1200, temperature=0.0
    )
    validation = parse_json(validation_text)
    gate_names = ("language", "religion", "sleep", "quality", "universality", "non_duplicate")
    if validation.get("status") != "PASS" or not all(validation.get(name) is True for name in gate_names):
        raise RuntimeError(
            "TOPIC_GATE_REJECTED: " + json.dumps(validation, ensure_ascii=False)
        )
    topic["selection_gate"] = validation
    return topic


def choose_valid_topic(history: list[dict], attempts: int = 3) -> dict:
    last_error: RuntimeError | None = None
    for _attempt in range(attempts):
        try:
            return choose_topic(history)
        except RuntimeError as exc:
            if "TOPIC_GATE_REJECTED" not in str(exc):
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
        headers={"Authorization": f"Bearer {key}"},
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
    raise RuntimeError("GROUNDED_RESEARCH_UNAVAILABLE: all search providers failed (" + "; ".join(failures) + ")")


def research_from_search_providers(topic: dict, prompt: str) -> tuple[str, list[dict]]:
    provider, results = _collect_sources(topic)
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


def research_topic(topic: dict) -> tuple[str, list[dict]]:
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
        research, sources = research_from_search_providers(topic, prompt)
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


def validate_story_quality(topic: dict, story_text: str) -> dict:
    prompt = f"""{production_rules(2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 23)}

أنت بوابة جودة مستقلة. افحص النص النهائي دون إعادة كتابته.
الموضوع: {topic['topic_ar']} — {topic['angle_ar']}

أعد JSON فقط:
{{"status":"PASS أو REJECT","language":true,"adult_audience":true,"religion":true,"sleep":true,"historical_style":true,"no_dangerous_drift":true,"issues":["..."]}}
كل قيمة true تعني نجاح البوابة. ارفض إذا كانت اللغة ثقيلة أو طفولية، أو أصبح المحتوى دينيًا بحتًا، أو صادمًا وغير مناسب للنوم.

النص:
{story_text[:90000]}
"""
    text, _ = gemini(prompt, json_mode=True, max_tokens=2200, temperature=0.0)
    result = parse_json(text)
    gates = ("language", "adult_audience", "religion", "sleep", "historical_style", "no_dangerous_drift")
    if result.get("status") != "PASS" or not all(result.get(name) is True for name in gates):
        raise RuntimeError("STORY_QUALITY_GATE_REJECTED: " + json.dumps(result, ensure_ascii=False))
    return result


def fact_check_story(topic: dict, research: str, story_text: str) -> tuple[str, dict]:
    rules = production_rules(26, 27, 28, 29)
    first_prompt = f"""{rules}

نفّذ Fact Check مستقلًا اعتمادًا فقط على البحث وخريطة الادعاءات. أعد JSON فقط:
{{"status":"PASS أو NEEDS_CORRECTION","verified":["..."],"uncertain":[{{"claim":"...","action":"rephrase أو remove","replacement":"..."}}],"corrected":[{{"original":"...","replacement":"...","source":"URL أو مرجع من البحث"}}],"unsupported_critical":["..."]}}

الموضوع: {topic['topic_ar']}
البحث:
{research[:50000]}

القصة:
{story_text[:90000]}
"""
    first_text, _ = gemini(first_prompt, json_mode=True, max_tokens=8000, temperature=0.0)
    first = parse_json(first_text)
    uncertain = first.get("uncertain") or []
    corrected = first.get("corrected") or []
    unsupported = first.get("unsupported_critical") or []
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

هذا هو التحقق النهائي الثاني بعد تطبيق التصحيحات. اعتمد فقط على البحث. أعد JSON فقط:
{{"status":"PASS أو FAIL","remaining_errors":[],"unsupported_critical":[],"uncertain_presented_as_fact":[],"corrections_applied":true}}

البحث:
{research[:50000]}

القصة النهائية:
{final_story[:90000]}
"""
    second_text, _ = gemini(second_prompt, json_mode=True, max_tokens=5000, temperature=0.0)
    second = parse_json(second_text)
    passed = (
        second.get("status") == "PASS"
        and second.get("corrections_applied") is True
        and not (second.get("remaining_errors") or [])
        and not (second.get("unsupported_critical") or [])
        and not (second.get("uncertain_presented_as_fact") or [])
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
    quality = validate_story_quality(topic, draft)
    final_story, fact_check = fact_check_story(topic, research, draft)
    final_quality = validate_story_quality(topic, final_story)
    write(final_path, final_story)
    write(manifest_path, json.dumps({
        "draft_sha256": draft_hash,
        "final_sha256": hashlib.sha256(final_story.encode("utf-8")).hexdigest(),
        "quality_gate": final_quality,
        "fact_check": fact_check,
    }, ensure_ascii=False, indent=2))
    return final_story, final_quality, fact_check


def make_outline(topic: dict, research: str) -> list[dict]:
    rules = production_rules(17, 22)
    prompt = f"""التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

أنشئ مخططًا من 9 أقسام لسرد تاريخي عربي هادئ مخصص للنوم عن:
{topic['topic_ar']} — {topic['angle_ar']}

ملاحظات البحث:
{research[:45000]}

التوزيع: 60% حياة يومية ومكان وبيئة، 30% تاريخ وتجارة وتغيرات، 10% اكتشافات أثرية هادئة. البداية تدخل مباشرة في المكان والزمن بلا ترحيب أو تعليمات استرخاء. لا clickbait ولا خيال.
أعد JSON فقط: {{"sections":[{{"title":"...","brief":"...","target_words":700}}]}}.
اجعل الأقسام غير متكررة ومجموع target_words قرابة 6300 كلمة عربية.
"""
    text, _ = gemini(prompt, json_mode=True, max_tokens=3000, temperature=0.3)
    result = parse_json(text)
    sections = result.get("sections") or []
    if len(sections) < 7:
        raise RuntimeError("Outline contains too few sections")
    return sections


def draft_story(topic: dict, research: str, sections: list[dict], log_dir: Path) -> str:
    rules = production_rules(2, 3, 4, 5, 6, 7, 17, 23)
    chapters: list[str] = []
    for index, section in enumerate(sections, 1):
        previous = chapters[-1][-2500:] if chapters else "هذه بداية السرد."
        chapter_path = log_dir / f"chapter_{index:02d}.md"
        target_words = max(850, int(section.get("target_words", 700)))
        prompt = f"""التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

اكتب القسم {index} من {len(sections)} من قصة تاريخية حقيقية طويلة للنوم.
الموضوع: {topic['topic_ar']}
الزاوية: {topic['angle_ar']}
عنوان القسم: {section.get('title', '')}
المطلوب: {section.get('brief', '')}
الهدف: بين {target_words} و{target_words + 150} كلمة عربية.

المصدر المعرفي:
{research[:42000]}

نهاية القسم السابق للسياق فقط:
{previous}

اكتب السرد وحده بلا عنوان وبلا ملاحظات تحريرية. فصحى سهلة، دافئة وبطيئة ومتصلة. لا تبدأ بترحيب أو «في هذا الوثائقي» أو تعليمات تنفس. لا تخترع تفاصيل. صِغ المسائل المختلف عليها بحذر. تجنب الإثارة والدموية والخاتمة من نوع «شكرًا على الاستماع».
"""
        if chapter_path.exists():
            chapter = chapter_path.read_text(encoding="utf-8").strip()
        else:
            chapter, _ = gemini(prompt, max_tokens=7000, temperature=0.45)
            # Save the first usable response before any optional expansion. A
            # hosted runner can then restore and expand it after a transient
            # provider failure instead of generating the chapter again.
            write(chapter_path, chapter)
        if word_count(chapter) < 600:
            expansion_prompt = f"""أعد كتابة المسودة التالية ووسّعها إلى ما بين {target_words} و{target_words + 150} كلمة عربية.
حافظ على الحقائق والنبرة الهادئة وتسلسل الفقرة، وأضف فقط تفاصيل يومية وتاريخية يدعمها المصدر المعرفي. لا تضف عنوانًا ولا مقدمة للمستمع ولا خاتمة شكر.

الموضوع: {topic['topic_ar']}
القسم: {section.get('title', '')}
المصدر المعرفي:
{research[:36000]}

المسودة:
{chapter}
"""
            expanded, _ = gemini(expansion_prompt, max_tokens=8000, temperature=0.35)
            if word_count(expanded) > word_count(chapter):
                chapter = expanded
                write(chapter_path, chapter)
        # Rewrites tend to compress, so a still-short chapter is extended by
        # appending continuation paragraphs, checkpointing each improvement.
        for _attempt in range(3):
            if word_count(chapter) >= 600:
                break
            missing = max(250, target_words - word_count(chapter))
            continuation_prompt = f"""التزم بالقواعد التالية التزامًا إلزاميًا:
{rules}

أكمل القسم التالي من قصة تاريخية للنوم بفقرات جديدة تأتي بعد نهايته مباشرة، بنحو {missing} كلمة عربية.
لا تكرر ما سبق ولا تلخصه، ولا تضف عنوانًا أو خاتمة شكر. أضف فقط تفاصيل يومية وتاريخية يدعمها المصدر المعرفي، بنفس النبرة الهادئة.

الموضوع: {topic['topic_ar']}
القسم: {section.get('title', '')} — {section.get('brief', '')}
المصدر المعرفي:
{research[:36000]}

نص القسم حتى الآن:
{chapter}
"""
            continuation, _ = gemini(continuation_prompt, max_tokens=4000, temperature=0.4)
            if word_count(continuation) > 0:
                chapter = f"{chapter.rstrip()}\n\n{continuation.strip()}"
                write(chapter_path, chapter)
        if word_count(chapter) < 600:
            raise RuntimeError(f"Chapter {index} is too short after expansion: {word_count(chapter)} words")
        write(chapter_path, chapter)
        chapters.append(chapter)
    return "\n\n".join(chapters)


def resume_partial_story(story: Path) -> Path:
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
    else:
        research, sources = research_topic(topic)
        write(research_path, research)
        write(sources_path, json.dumps(sources, ensure_ascii=False, indent=2))
    if outline_path.exists():
        sections = json.loads(outline_path.read_text(encoding="utf-8"))["sections"]
    else:
        sections = make_outline(topic, research)
        write(outline_path, json.dumps({"sections": sections}, ensure_ascii=False, indent=2))
    draft = draft_story(topic, research, sections, story / "logs")
    if word_count(draft) < 5000:
        raise RuntimeError(f"Generated story is too short: {word_count(draft)} Arabic words")
    write(story / "logs/assembled_story.md", draft)
    story_text, quality_gate, fact_check = finalize_story_text(story, topic, research, draft)
    build_metadata_files(story, topic, research, sources, story_text, quality_gate, fact_check)
    history_path = ROOT / "config/topics_history.json"
    upsert_history_checkpoint(history_path, story, topic, "drafting")
    print(json.dumps({"ok": True, "story_path": str(story), "slug": topic["slug"]}, ensure_ascii=False))
    return story


def build_metadata_files(story: Path, topic: dict, research: str, sources: list[dict], story_text: str,
                         quality_gate: dict, fact_check: dict) -> None:
    today = dt.date.today().isoformat()
    source_lines = ["# المصادر", "", f"تاريخ الوصول: {today}", ""]
    for index, source in enumerate(sources[:12], 1):
        source_lines += [f"## {index}. {source['title']}", "", f"- الرابط: {source['url']}", "- الاستخدام: التحقق من السياق التاريخي والآثاري الوارد في ملاحظات البحث.", ""]
    if not sources:
        source_lines += ["- راجع الروابط المباشرة الواردة داخل `04_research_notes.md` قبل الاعتماد."]

    title_rules = production_rules(44)
    title_prompt = f"""{title_rules}

أنشئ عنوان YouTube عربيًا واحدًا صادقًا وهادئًا للنوم عن «{topic['topic_ar']}» وفق قالب مثل «ستنام قبل النهاية 😴 | ...» أو «لن تُكمل هذه القصة 😴 | ...». أعد العنوان فقط، بلا اقتباس وبحد أقصى 95 حرفًا."""
    title, _ = gemini(title_prompt, max_tokens=250, temperature=0.5)
    title = title.splitlines()[0].strip().strip('"')

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
    image_prompt = f"""16:9 horizontal YouTube background, 1920x1080, ultra realistic cinematic historical reconstruction of {topic['search_query_en']}, calm quiet night, soft warm low lighting, subtle moonlight, sleep-inducing composition, historically plausible environment, no action, no text, no logo, no watermark, no fantasy, no modern objects."""

    write(story / "01_title.txt", title)
    write(story / "02_description.txt", description)
    write(story / "03_sources.md", "\n".join(source_lines))
    write(story / "04_research_notes.md", research)
    write(story / "05_story_clean.md", story_text)
    write(story / "06_story_tts_annotated.md", annotated)
    write(story / "07_story_tts_api.md", api_text)
    write(story / "08_image_prompt_en.txt", image_prompt)
    write(story / "09_image_source.md", "# مصدر الصورة\n\nImage generation pending")
    write(story / "16_fact_check.md", "# Fact Check\n\n```json\n" + json.dumps(fact_check, ensure_ascii=False, indent=2) + "\n```")
    write(story / "17_status.md", "# الحالة\n\n- الحالة الحالية: `drafting`\n- النشر: `not_published`.\n- سيحدّث خط الإنتاج هذه الحالة بعد اكتمال الصوت والفيديو والتحقق.")
    metadata = {
        "title": title,
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
        "approval_status": "drafting",
        "publish_status": "not_published",
        "youtube_video_id": None,
    }
    write(story / "15_metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))


def create_daily_story(dry_run: bool = False) -> Path | None:
    history_path = ROOT / "config/topics_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    if not dry_run:
        def checkpoint_path(item: dict) -> Path | None:
            raw = item.get("story_path")
            if not raw:
                return None
            candidate = Path(raw)
            return candidate if candidate.is_absolute() else ROOT / candidate

        # Prefer the newest unfinished checkpoint. A GitHub-hosted runner is
        # ephemeral, so its catalogue commit can survive while the draft
        # directory is gone. In that case rebuild the same topic instead of
        # silently selecting a different episode.
        unfinished = next((
            item for item in reversed(history)
            if (
                item.get("status") in {"generating", "drafting"}
                or (
                    item.get("status") == "pending_approval"
                    and not item.get("archive_identifier")
                )
                or (
                    item.get("status") == "READY_FOR_APPROVAL"
                    and not item.get("archive_identifier")
                )
            )
            and checkpoint_path(item)
        ), None)
        if unfinished:
            saved_story = checkpoint_path(unfinished).resolve()
            if saved_story.exists():
                if unfinished.get("status") == "READY_FOR_APPROVAL" and (saved_story / "15_metadata.json").exists():
                    print(json.dumps({"ok": True, "story_path": str(saved_story), "slug": unfinished["slug"]}, ensure_ascii=False))
                    return saved_story
                return resume_partial_story(saved_story)
            topic = topic_from_checkpoint(unfinished)
            story = saved_story
        else:
            topic = choose_valid_topic(history)
            story = ROOT / "stories" / f"{dt.date.today().isoformat()}_{topic['slug']}"
    else:
        topic = choose_valid_topic(history)
        story = ROOT / "stories" / f"{dt.date.today().isoformat()}_{topic['slug']}"
    if dry_run:
        print(json.dumps({"ok": True, "mode": "dry-run", "topic": topic}, ensure_ascii=False))
        return None
    if story.exists():
        raise RuntimeError(f"Story directory already exists: {story}")
    story.mkdir(parents=True)
    (story / "logs").mkdir()
    write(story / "logs/topic.json", json.dumps(topic, ensure_ascii=False, indent=2))
    # Register the selected topic before the first long research request. If a
    # provider call fails later, the next run resumes this exact directory and
    # its saved chapters instead of selecting a different topic.
    upsert_history_checkpoint(history_path, story, topic, "generating")
    research, sources = research_topic(topic)
    write(story / "logs/research_notes_draft.md", research)
    write(story / "logs/research_grounding_sources.json", json.dumps(sources, ensure_ascii=False, indent=2))
    outline = make_outline(topic, research)
    write(story / "logs/outline.json", json.dumps({"sections": outline}, ensure_ascii=False, indent=2))
    draft = draft_story(topic, research, outline, story / "logs")
    if word_count(draft) < 5000:
        raise RuntimeError(f"Generated story is too short: {word_count(draft)} Arabic words")
    write(story / "logs/assembled_story.md", draft)
    story_text, quality_gate, fact_check = finalize_story_text(story, topic, research, draft)
    build_metadata_files(story, topic, research, sources, story_text, quality_gate, fact_check)
    upsert_history_checkpoint(history_path, story, topic, "drafting")
    print(json.dumps({"ok": True, "story_path": str(story), "slug": topic["slug"]}, ensure_ascii=False))
    return story


def main() -> None:
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
