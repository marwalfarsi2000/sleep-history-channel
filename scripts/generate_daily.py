from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
SECRETS_FILE = Path(os.environ.get(
    "SLEEP_HISTORY_SECRETS_FILE",
    SETTINGS.get(
        "secrets_file",
        str(Path.home() / "AppData/Local/SleepHistoryAutomation/secrets.env"),
    ),
))
MODEL = SETTINGS.get("llm_model", "gemini-flash-latest")

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


def gemini(prompt: str, *, json_mode: bool = False, search: bool = False,
           max_tokens: int = 8192, temperature: float = 0.4) -> tuple[str, list[dict]]:
    key = load_secrets().get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError(f"GEMINI_API_KEY is missing from {SECRETS_FILE}")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
        f"?key={key}"
    )
    config: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        "thinkingConfig": {"thinkingBudget": 0},
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
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            last_error = f"Gemini HTTP {error.code}: {body[:600]}"
            if attempt == 3 and search and error.code in (429, 503):
                break
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError(last_error) from error
            time.sleep((2, 5, 12)[attempt])
    if result is None and search and ("HTTP 429" in last_error or "HTTP 503" in last_error):
        fallback_payload = dict(payload)
        fallback_payload.pop("tools", None)
        fallback_payload["contents"] = [{
            "role": "user",
            "parts": [{
                "text": (
                    "تعذر البحث المباشر بسبب حصة Google Search المؤقتة. "
                    "اكتب البحث اعتمادًا على معرفتك الموثوقة فقط، واذكر أسماء وروابط "
                    "المؤسسات والمتاحف والجامعات التي ينبغي مراجعتها. لا تخترع رابطًا أو اقتباسًا.\n\n"
                    + prompt
                )
            }],
        }]
        fallback_request = urllib.request.Request(
            url,
            data=json.dumps(fallback_payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(fallback_request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini fallback HTTP {error.code}: {body[:600]}") from error
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
    return text, sources


def parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    return json.loads(cleaned)


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:70] or f"history-{dt.date.today():%Y%m%d}"


def word_count(text: str) -> int:
    return len(re.findall(r"[\u0600-\u06FF]+", text))


def write(path: Path, content: str) -> None:
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


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
    write(history_path, json.dumps(history, ensure_ascii=False, indent=2))


def choose_topic(history: list[dict]) -> dict:
    prior = "\n".join(
        f"- {item.get('topic', item.get('slug', ''))} | {item.get('angle', '')}"
        for item in history
    ) or "لا يوجد سجل سابق"
    prompt = f"""أنت محرر قناة عربية اسمها «لن تُكمل هذه القصة» تقدم سردًا تاريخيًا حقيقيًا هادئًا للنوم.
اختر موضوع الحلقة اليومية التالية. يجب أن تتوفر له مصادر متحفية أو جامعية أو أثرية كثيرة، وأن يسمح بوصف الحياة اليومية والمكان لمدة ساعة، وألا يكرر موضوعًا أو زاوية في السجل.

السجل السابق:
{prior}

أعد JSON فقط بهذه المفاتيح الإنجليزية:
topic_ar: اسم موضوع واضح بالعربية
angle_ar: زاوية الحلقة
slug: حروف إنجليزية صغيرة وشرطات فقط
search_query_en: استعلام بحث إنجليزي دقيق
period_ar: الفترة التاريخية
reason_ar: سبب الاختيار في جملة
"""
    text, _ = gemini(prompt, json_mode=True, max_tokens=1200, temperature=0.7)
    topic = parse_json(text)
    topic["slug"] = safe_slug(str(topic.get("slug", "")))
    return topic


def research_topic(topic: dict) -> tuple[str, list[dict]]:
    prompt = f"""ابحث بحثًا تاريخيًا موثقًا للحلقة التالية:
الموضوع: {topic['topic_ar']}
الزاوية: {topic['angle_ar']}
الفترة: {topic['period_ar']}

استخدم مصادر أولية أو موثوقة قدر الإمكان: UNESCO، متاحف، جامعات، دوريات وأبحاث أثرية، ومؤسسات تراث رسمية. ويكيبيديا للاكتشاف فقط وليست المصدر الوحيد.
اكتب ملاحظات بحث عربية مفصلة ومنظمة تشمل: المكان والزمن، البيئة، البيوت، الطعام، الملابس، الحرف، التجارة، النقل، الإدارة، المعتقدات اللازمة للفهم، الأدلة الأثرية، التسلسل التاريخي، وما هو مختلف عليه.
أنشئ في النهاية قسمًا بعنوان «خريطة الادعاءات» يربط الادعاءات المهمة بالمصادر المذكورة. لا تخترع أسماء أو تواريخ أو اقتباسات. ضع روابط المصادر صراحة داخل النص أيضًا.
"""
    return gemini(prompt, search=True, max_tokens=12000, temperature=0.2)


def make_outline(topic: dict, research: str) -> list[dict]:
    prompt = f"""أنشئ مخططًا من 9 أقسام لسرد تاريخي عربي هادئ مخصص للنوم عن:
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
    chapters: list[str] = []
    for index, section in enumerate(sections, 1):
        previous = chapters[-1][-2500:] if chapters else "هذه بداية السرد."
        chapter_path = log_dir / f"chapter_{index:02d}.md"
        target_words = max(850, int(section.get("target_words", 700)))
        prompt = f"""اكتب القسم {index} من {len(sections)} من قصة تاريخية حقيقية طويلة للنوم.
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
            chapter, _ = gemini(expansion_prompt, max_tokens=8000, temperature=0.35)
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
    story_text = draft_story(topic, research, sections, story / "logs")
    if word_count(story_text) < 5000:
        raise RuntimeError(f"Generated story is too short: {word_count(story_text)} Arabic words")
    build_metadata_files(story, topic, research, sources, story_text)
    history_path = ROOT / "config/topics_history.json"
    upsert_history_checkpoint(history_path, story, topic, "drafting")
    print(json.dumps({"ok": True, "story_path": str(story), "slug": topic["slug"]}, ensure_ascii=False))
    return story


def build_metadata_files(story: Path, topic: dict, research: str, sources: list[dict], story_text: str) -> None:
    today = dt.date.today().isoformat()
    source_lines = ["# المصادر", "", f"تاريخ الوصول: {today}", ""]
    for index, source in enumerate(sources[:12], 1):
        source_lines += [f"## {index}. {source['title']}", "", f"- الرابط: {source['url']}", "- الاستخدام: التحقق من السياق التاريخي والآثاري الوارد في ملاحظات البحث.", ""]
    if not sources:
        source_lines += ["- راجع الروابط المباشرة الواردة داخل `04_research_notes.md` قبل الاعتماد."]

    title_prompt = f"""أنشئ عنوان YouTube عربيًا واحدًا صادقًا وهادئًا للنوم عن «{topic['topic_ar']}» وفق قالب مثل «ستنام قبل النهاية 😴 | ...» أو «لن تُكمل هذه القصة 😴 | ...». أعد العنوان فقط، بلا اقتباس وبحد أقصى 95 حرفًا."""
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

    fact_prompt = f"""نفّذ تدقيقًا تاريخيًا ثانيًا مستقلًا للنص التالي اعتمادًا على ملاحظات البحث. استخرج الأسماء والتواريخ والأرقام والمواقع والادعاءات الأثرية. أعد تقرير Markdown بثلاثة أقسام VERIFIED وUNCERTAIN وCORRECTED. لا تدّعِ التحقق مما لا تدعمه الملاحظات.

ملاحظات البحث:
{research[:45000]}

النص:
{story_text[:80000]}
"""
    fact_check, _ = gemini(fact_prompt, max_tokens=7000, temperature=0.1)

    write(story / "01_title.txt", title)
    write(story / "02_description.txt", description)
    write(story / "03_sources.md", "\n".join(source_lines))
    write(story / "04_research_notes.md", research)
    write(story / "05_story_clean.md", story_text)
    write(story / "06_story_tts_annotated.md", annotated)
    write(story / "07_story_tts_api.md", api_text)
    write(story / "08_image_prompt_en.txt", image_prompt)
    write(story / "09_image_source.md", "# مصدر الصورة\n\n- الطريقة: ستُولد تلقائيًا بواسطة نموذج SDXL Lightning Free عبر Pollinations.\n- التكلفة: FREE مع Cost Guard.\n- ملاحظة: الخلفية تمثيل بصري مولد وليست إعادة بناء أثرية حرفية.")
    write(story / "16_fact_check.md", fact_check)
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
        "fact_check_status": "completed",
        "image_method": "Pollinations SDXL Lightning Free",
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

        unfinished = next((
            item for item in history
            if item.get("status") == "generating"
            and checkpoint_path(item)
            and checkpoint_path(item).exists()
        ), None)
        if unfinished:
            return resume_partial_story(checkpoint_path(unfinished).resolve())
    topic = choose_topic(history)
    if dry_run:
        print(json.dumps({"ok": True, "mode": "dry-run", "topic": topic}, ensure_ascii=False))
        return None
    date_prefix = dt.date.today().isoformat()
    story = ROOT / "stories" / f"{date_prefix}_{topic['slug']}"
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
    story_text = draft_story(topic, research, outline, story / "logs")
    if word_count(story_text) < 5000:
        raise RuntimeError(f"Generated story is too short: {word_count(story_text)} Arabic words")
    build_metadata_files(story, topic, research, sources, story_text)
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
