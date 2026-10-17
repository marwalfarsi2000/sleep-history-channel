"""Packaging: quota != validation, targeted field retry, deterministic suffix, checkpoint resume."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

import generate_daily
import llm_router
from llm_router import LlmQuotaWaiting

TOPIC = {
    "slug": "oasis-life-gulf-falaj-palm",
    "topic_ar": "الحياة اليومية في واحة الموتس عبر طريق اللؤلؤ والتمور في الخليج العربي",
    "angle_ar": "تتبع يوم في حياة مزارع نخيل ونظام العيون والأفلاج",
    "period_ar": "القرن الثامن عشر الميلادي",
    "location_ar": "شبه الجزيرة العربية والخليج",
    "family": "الطبيعة التاريخية والبيئة",
}
GOOD_CORE = "ظل النخيل وماء الأفلاج في واحة خليجية"
GOOD_TEXT = "ماء الأفلاج"
GOOD_PROMPT = (
    "A quiet date-palm oasis at dusk with a falaj water channel and warm lantern light, "
    "calm illustrated realism. Background scene only, no text, no logo, no watermark. "
    "16:9 YouTube thumbnail. Professional quality."
)
TOO_LONG_CORE = "ظل النخيل وماء الأفلاج وحياة المزارعين اليومية في واحة قديمة على ساحل الخليج العربي"
PASS = json.dumps({"status": "PASS", "title": True, "thumbnail_text": True, "thumbnail_prompt": True,
                   "failing_fields": [], "issues": []})


@pytest.fixture
def story(tmp_path, monkeypatch):
    folder = tmp_path / "stories" / "2026-10-17_oasis-life-gulf-falaj-palm"
    (folder / "logs").mkdir(parents=True)
    rules = tmp_path / "rules.md"
    rules.write_text("packaging rules", encoding="utf-8")
    monkeypatch.setattr(generate_daily, "YOUTUBE_PACKAGING_RULES_PATH", rules)
    monkeypatch.setattr(generate_daily, "recent_published_packaging", lambda limit=10: [])
    return folder


def package_json(**fields):
    return json.dumps(fields, ensure_ascii=False)


def requested_keys(prompt: str) -> list[str]:
    marker = "exactly these keys: "
    return json.loads(prompt.split(marker)[1].splitlines()[0]) if marker in prompt else []


def test_invalid_title_content_still_fails_real_packaging_validation_with_the_exact_reason(story):
    responses = [(package_json(episode_title_core=TOO_LONG_CORE, thumbnail_text=GOOD_TEXT, thumbnail_prompt=GOOD_PROMPT), [])]
    responses += [(package_json(episode_title_core=TOO_LONG_CORE), [])] * 5
    with mock.patch.object(generate_daily, "gemini", side_effect=responses):
        with pytest.raises(RuntimeError) as raised:
            generate_daily.generate_youtube_packaging(story, TOPIC)
    message = str(raised.value)
    assert message.startswith("YOUTUBE_PACKAGING_VALIDATION_FAILED: episode_title_core")
    assert "Arabic words" in message


def test_only_the_failing_field_is_regenerated_and_valid_fields_survive(story):
    prompts = []
    responses = iter([
        (package_json(episode_title_core=TOO_LONG_CORE, thumbnail_text=GOOD_TEXT, thumbnail_prompt=GOOD_PROMPT), []),
        (package_json(episode_title_core=GOOD_CORE), []),
        (PASS, []),
    ])

    def fake(prompt, **kwargs):
        prompts.append(prompt)
        return next(responses)

    with mock.patch.object(generate_daily, "gemini", side_effect=fake):
        package = generate_daily.generate_youtube_packaging(story, TOPIC)
    assert requested_keys(prompts[0]) == ["episode_title_core", "thumbnail_text", "thumbnail_prompt"]
    assert requested_keys(prompts[1]) == ["episode_title_core"]          # only the failing field
    assert package["thumbnail_text"] == GOOD_TEXT and package["thumbnail_prompt"] == GOOD_PROMPT
    assert package["episode_title_core"] == GOOD_CORE
    assert package["final_youtube_title"] == f"{GOOD_CORE} | {package['title_suffix']}"


def test_final_title_and_suffix_are_computed_by_code_not_taken_from_the_model(story):
    responses = iter([
        (package_json(episode_title_core=GOOD_CORE, thumbnail_text=GOOD_TEXT, thumbnail_prompt=GOOD_PROMPT,
                      title_suffix="لاحقة مخترعة", final_youtube_title="عنوان مخترع"), []),
        (PASS, []),
    ])
    with mock.patch.object(generate_daily, "gemini", side_effect=lambda *a, **k: next(responses)):
        package = generate_daily.generate_youtube_packaging(story, TOPIC)
    assert package["title_suffix"] in generate_daily.TITLE_SUFFIXES
    assert package["final_youtube_title"] == f"{GOOD_CORE} | {package['title_suffix']}"
    assert package["final_youtube_title"].count("😴") == 1 and len(package["final_youtube_title"]) <= 95


def test_suffix_selection_is_deterministic_rotating_and_avoids_recent_history():
    suffixes = generate_daily.TITLE_SUFFIXES
    assert generate_daily.choose_title_suffix(TOPIC, []) == generate_daily.choose_title_suffix(TOPIC, [])
    recent = [{"title_suffix": s} for s in suffixes[:3]]
    assert generate_daily.choose_title_suffix(TOPIC, recent) not in suffixes[:3]
    # With the last three used and everything else used once, the least-used one is chosen.
    heavy = [{"title_suffix": suffixes[0]}] * 4 + [{"title_suffix": s} for s in suffixes[1:7]]
    assert generate_daily.choose_title_suffix(TOPIC, heavy) in (suffixes[7],)
    # A rejected suffix is never offered again.
    first = generate_daily.choose_title_suffix(TOPIC, [])
    assert generate_daily.choose_title_suffix(TOPIC, [], rejected=[first]) != first


def test_travel_topics_prefer_journey_suffixes():
    journey = dict(TOPIC, topic_ar="قافلة على طريق الحرير", angle_ar="رحلة تاجر")
    assert "رحلة" in generate_daily.choose_title_suffix(journey, [])


def test_provider_quota_is_never_reported_as_a_packaging_validation_error(story):
    waiting = LlmQuotaWaiting("WAITING_FOR_LLM_QUOTA: blocked", kind="daily", scope="model",
                              blocked_until="2026-10-18T07:00:00+00:00", models_attempted=["m"], daily=True)
    with mock.patch.object(generate_daily, "gemini", side_effect=waiting):
        with pytest.raises(LlmQuotaWaiting) as raised:
            generate_daily.generate_youtube_packaging(story, TOPIC)
    assert "YOUTUBE_PACKAGING_VALIDATION_FAILED" not in str(raised.value)
    # Progress was saved before the provider call, with the deterministic suffix already fixed.
    partial = json.loads((story / "logs" / "youtube_packaging_partial.json").read_text(encoding="utf-8"))
    assert partial["package"]["title_suffix"] in generate_daily.TITLE_SUFFIXES


def test_quota_in_the_middle_resumes_at_the_exact_failing_field(story):
    calls = []
    first = iter([
        (package_json(episode_title_core=TOO_LONG_CORE, thumbnail_text=GOOD_TEXT, thumbnail_prompt=GOOD_PROMPT), []),
        LlmQuotaWaiting("WAITING_FOR_LLM_QUOTA", kind="per_minute_model", scope="model",
                        blocked_until="2026-10-18T07:00:00+00:00", models_attempted=["m"]),
    ])

    def interrupted(prompt, **kwargs):
        calls.append(prompt)
        item = next(first)
        if isinstance(item, Exception):
            raise item
        return item

    with mock.patch.object(generate_daily, "gemini", side_effect=interrupted):
        with pytest.raises(LlmQuotaWaiting):
            generate_daily.generate_youtube_packaging(story, TOPIC)
    second_prompts = []
    second = iter([(package_json(episode_title_core=GOOD_CORE), []), (PASS, [])])

    def resumed(prompt, **kwargs):
        second_prompts.append(prompt)
        return next(second)

    with mock.patch.object(generate_daily, "gemini", side_effect=resumed):
        package = generate_daily.generate_youtube_packaging(story, TOPIC)
    assert requested_keys(second_prompts[0]) == ["episode_title_core"]     # text + prompt were kept
    assert package["thumbnail_text"] == GOOD_TEXT and package["episode_title_core"] == GOOD_CORE


def test_semantic_gate_rejection_regenerates_only_the_rejected_field(story):
    prompts = []
    responses = iter([
        (package_json(episode_title_core=GOOD_CORE, thumbnail_text=GOOD_TEXT, thumbnail_prompt=GOOD_PROMPT), []),
        (json.dumps({"status": "FAIL", "title": True, "thumbnail_text": True, "thumbnail_prompt": False,
                     "failing_fields": ["thumbnail_prompt"], "issues": ["generic city"]}), []),
        (package_json(thumbnail_prompt=GOOD_PROMPT.replace("oasis", "palm grove")), []),
        (PASS, []),
    ])

    def fake(prompt, **kwargs):
        prompts.append(prompt)
        return next(responses)

    with mock.patch.object(generate_daily, "gemini", side_effect=fake):
        package = generate_daily.generate_youtube_packaging(story, TOPIC)
    assert requested_keys(prompts[2]) == ["thumbnail_prompt"]
    assert package["episode_title_core"] == GOOD_CORE and package["thumbnail_text"] == GOOD_TEXT


# ---- a finished story must survive a packaging outage (Run #49 shape) -----------------------------

def arabic_words(count: int, seed: str) -> str:
    return " ".join(f"{seed}{'ابجدهوز'[i % 7]}" for i in range(count))


@pytest.fixture
def oasis(tmp_path, monkeypatch):
    """Story folder as the Run #49 failure artifact left it: research, outline, chapters and gates done."""
    root = tmp_path
    (root / "config").mkdir()
    folder = root / "stories" / "2026-10-17_oasis-life-gulf-falaj-palm"
    logs = folder / "logs"
    logs.mkdir(parents=True)
    (root / "config" / "youtube_description_template.txt").write_text("{{TOPIC_BLOCK}}", encoding="utf-8")
    rules = root / "config" / "youtube_packaging_rules.md"
    rules.write_text("rules", encoding="utf-8")
    history = [{"slug": TOPIC["slug"], "status": "GENERATING", "story_path": folder.relative_to(root).as_posix(),
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}]
    (root / "config" / "topics_history.json").write_text(json.dumps(history), encoding="utf-8")
    (logs / "topic.json").write_text(json.dumps(TOPIC, ensure_ascii=False), encoding="utf-8")
    (logs / "research_notes_draft.md").write_text("بحث https://a.org/1 https://b.org/2 https://c.org/3", encoding="utf-8")
    (logs / "research_grounding_sources.json").write_text(json.dumps(
        [{"title": "a", "url": "https://a.org/1"}, {"title": "b", "url": "https://b.org/2"}]), encoding="utf-8")
    sections = [{"title": f"قسم {i}", "brief": "x", "target_words": 700} for i in range(1, 10)]
    (logs / "outline.json").write_text(json.dumps({"sections": sections}), encoding="utf-8")
    chapters = []
    for i in range(1, 10):
        text = arabic_words(700, f"فصل{i}")
        (logs / f"chapter_{i:02d}.md").write_text(text, encoding="utf-8")
        chapters.append(text)
    draft = "\n\n".join(chapters)
    final = draft + "\n\nخاتمة"
    (logs / "final_story_after_gates.md").write_text(final, encoding="utf-8")
    (logs / "content_gates.json").write_text(json.dumps({
        "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        "quality_gate": {"status": "PASS"}, "fact_check": {"status": "PASS"},
    }), encoding="utf-8")
    monkeypatch.setattr(generate_daily, "ROOT", root)
    monkeypatch.setattr(generate_daily, "YOUTUBE_PACKAGING_RULES_PATH", rules)
    monkeypatch.setattr(generate_daily, "recent_published_packaging", lambda limit=10: [])
    monkeypatch.setattr(generate_daily, "production_rules", lambda *a: "")
    for name in ("research_topic", "make_outline", "draft_story_chapter", "validate_story_quality", "fact_check_story"):
        if hasattr(generate_daily, name):
            monkeypatch.setattr(generate_daily, name, mock.Mock(side_effect=AssertionError(f"{name} must not run again")))
    return root, folder


def test_packaging_outage_parks_the_episode_and_the_next_run_resumes_packaging_only(oasis):
    root, folder = oasis
    outage = LlmQuotaWaiting("WAITING_FOR_LLM_QUOTA: all blocked", kind="per_minute_model", scope="model",
                             blocked_until="2026-10-18T07:00:00+00:00", models_attempted=["m"])
    with mock.patch.object(generate_daily, "gemini", side_effect=outage) as gemini:
        with pytest.raises(LlmQuotaWaiting):
            generate_daily.resume_partial_story(folder)
    assert gemini.call_count == 1                                    # the very first call was a packaging call
    assert (folder / "logs" / "story_ready_for_packaging.json").exists()
    assert not (folder / "15_metadata.json").exists()

    responses = iter([
        (package_json(episode_title_core=GOOD_CORE, thumbnail_text=GOOD_TEXT, thumbnail_prompt=GOOD_PROMPT), []),
        (PASS, []),
    ])
    with mock.patch.object(generate_daily, "gemini", side_effect=lambda *a, **k: next(responses)) as gemini:
        result = generate_daily.resume_partial_story(folder)
    assert result == folder and gemini.call_count == 2               # package + semantic gate, nothing else
    metadata = json.loads((folder / "15_metadata.json").read_text(encoding="utf-8"))
    assert metadata["slug"] == TOPIC["slug"] and metadata["thumbnail_text"] == GOOD_TEXT
    history = json.loads((root / "config" / "topics_history.json").read_text(encoding="utf-8"))
    assert history[0]["status"] == "GENERATING"
