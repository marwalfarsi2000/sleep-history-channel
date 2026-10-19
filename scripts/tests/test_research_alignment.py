"""RESEARCH_TOPIC_ALIGNMENT_GATE: sources must support the narrative spine before any outline."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

import generate_daily
import research_alignment as ra

TOPIC = {
    "slug": "tripoli-lybia-karamanli-daily-life", "topic_ar": "الحياة اليومية في طرابلس الغرب", "period_ar": "القرن الثامن عشر",
    "location_ar": "طرابلس", "story_format": "CRAFT_QUEST",
    "angle_ar": "عائلة تحضّر خيوط الصوف وتصبغها وتنسج السجاد", "narrative_follow": "عائلة تنسج الصوف",
    "soft_narrative_spine": "صبغ الخيوط والزيوت العطرية", "gentle_curiosity": "كيف صُبغت الخيوط؟",
    "key_elements": ["السجاد", "الصوف", "صبغ الخيوط", "الزيوت العطرية"],
}
REANCHORED = {
    "angle_ar": "تتبع ضوء النهار على الميناء والبيوت البيضاء", "narrative_follow": "حركة الميناء من الصباح إلى المساء",
    "soft_narrative_spine": "تدرج الضوء على المدينة القديمة", "gentle_curiosity": "كيف كان الميناء يخدم المدينة؟",
    "key_elements": ["الميناء", "البيوت البيضاء", "الأسواق"],
}
RESEARCH = "ملاحظات بحث\n## خريطة الادعاءات\nhttps://a.example/1 https://b.example/2 https://c.example/3"


def verdict(status, unsupported=(), supported=("الميناء",), reason="سبب"):
    return json.dumps({"status": status, "supported_elements": list(supported),
                       "unsupported_elements": list(unsupported), "reason": reason}, ensure_ascii=False), []


def gemini_of(*replies):
    queue = list(replies)
    calls = []

    def fake(prompt, **kwargs):
        calls.append(prompt)
        return queue.pop(0)
    fake.calls = calls
    return fake


def gate(fake, topic=None, cache=None, search_more=None):
    return ra.run_gate(fake, json.loads, topic or dict(TOPIC), RESEARCH, cache_path=cache, search_more=search_more)


def test_pass_keeps_the_topic_and_records_metadata():
    fake = gemini_of(verdict(ra.PASS))
    topic, research, record = gate(fake)
    assert record["research_topic_alignment_status"] == "PASS" and record["unsupported_topic_elements"] == []
    assert topic["angle_ar"] == TOPIC["angle_ar"] and research == RESEARCH and len(fake.calls) == 1
    assert set(ra.METADATA_KEYS) <= set(topic)


def test_unsupported_spine_is_reanchored_inside_the_same_episode():
    fake = gemini_of(
        verdict(ra.REANCHOR_REQUIRED, ["السجاد", "صبغ الخيوط", "الزيوت العطرية"]),
        (json.dumps(REANCHORED, ensure_ascii=False), []),
        verdict(ra.PASS, supported=REANCHORED["key_elements"]),
    )
    topic, _, record = gate(fake)
    assert topic["slug"] == TOPIC["slug"] and topic["topic_ar"] == TOPIC["topic_ar"]      # same episode
    assert topic["angle_ar"] == REANCHORED["angle_ar"] and topic["key_elements"] == REANCHORED["key_elements"]
    assert record["unsupported_topic_elements"] == ["السجاد", "صبغ الخيوط", "الزيوت العطرية"]
    assert record["reanchored_elements"] == REANCHORED["key_elements"]
    assert "re-anchored" in record["alignment_reason"]
    assert "السجاد" in fake.calls[1] and "أزلها" in fake.calls[1]          # told to drop the unsupported elements


def test_unsupported_elements_are_removed_from_the_checked_topic():
    fake = gemini_of(verdict(ra.REANCHOR_REQUIRED, ["السجاد"]), (json.dumps(REANCHORED, ensure_ascii=False), []),
                     verdict(ra.PASS))
    topic, _, _ = gate(fake)
    assert not set(topic["key_elements"]) & {"السجاد", "الصوف", "صبغ الخيوط", "الزيوت العطرية"}
    assert "الصوف" not in json.dumps({k: topic.get(k) for k in ra.REANCHORED_FIELDS}, ensure_ascii=False)


def test_a_pass_that_lists_unsupported_elements_is_not_trusted():
    fake = gemini_of(verdict(ra.PASS, ["السجاد"]), (json.dumps(REANCHORED, ensure_ascii=False), []), verdict(ra.PASS))
    topic, _, record = gate(fake)
    assert topic["angle_ar"] == REANCHORED["angle_ar"] and record["reanchored_elements"]


def test_insufficient_research_stops_with_a_resumable_error_and_never_invents():
    fake = gemini_of(verdict(ra.RESEARCH_INSUFFICIENT, ["كل شيء"]))
    with pytest.raises(ra.AlignmentError, match="RESEARCH_TOPIC_ALIGNMENT_INSUFFICIENT"):
        gate(fake)


def test_one_limited_extra_search_can_rescue_insufficient_research():
    extra = mock.Mock(return_value="بحث إضافي عن الميناء https://d.example/4")
    fake = gemini_of(verdict(ra.RESEARCH_INSUFFICIENT), verdict(ra.PASS))
    _, research, record = gate(fake, search_more=extra)
    assert extra.call_count == 1 and "بحث إضافي" in research and record["research_topic_alignment_status"] == "PASS"


def test_extra_search_is_used_at_most_once():
    extra = mock.Mock(return_value="إضافي")
    fake = gemini_of(verdict(ra.RESEARCH_INSUFFICIENT), verdict(ra.RESEARCH_INSUFFICIENT))
    with pytest.raises(ra.AlignmentError):
        gate(fake, search_more=extra)
    assert extra.call_count == 1


def test_reanchor_that_still_fails_is_not_accepted():
    fake = gemini_of(verdict(ra.REANCHOR_REQUIRED, ["x"]), (json.dumps(REANCHORED, ensure_ascii=False), []),
                     verdict(ra.REANCHOR_REQUIRED, ["y"]))
    with pytest.raises(ra.AlignmentError, match="INSUFFICIENT"):
        gate(fake)


def test_unusable_verdicts_fail_closed():
    with pytest.raises(ra.AlignmentError, match="RESEARCH_TOPIC_ALIGNMENT_FAILED"):
        gate(gemini_of(("not json", []), ('{"status": "MAYBE"}', [])))


def test_pass_is_cached_for_identical_topic_and_research(tmp_path):
    cache = tmp_path / "alignment.json"
    gate(gemini_of(verdict(ra.PASS)), cache=cache)
    fake = gemini_of()
    _, _, record = gate(fake, cache=cache)
    assert record["research_topic_alignment_status"] == "PASS" and fake.calls == []
    changed = dict(TOPIC, angle_ar="زاوية أخرى")
    with pytest.raises(IndexError):               # cache is keyed on the content: a changed angle is re-checked
        gate(gemini_of(), topic=changed, cache=cache)


# ---- wired into the generator: a hard gate before the outline ---------------------------------------

@pytest.fixture
def story(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    folder = tmp_path / "stories" / "2026-10-18_tripoli-lybia-karamanli-daily-life"
    (folder / "logs").mkdir(parents=True)
    (tmp_path / "config" / "topics_history.json").write_text(json.dumps([{
        "slug": TOPIC["slug"], "topic": TOPIC["topic_ar"], "angle": TOPIC["angle_ar"], "status": "GENERATING",
        "story_path": folder.relative_to(tmp_path).as_posix(), "created_at": "2026-10-19T02:15:34+04:00"}]), encoding="utf-8")
    (folder / "logs/topic.json").write_text(json.dumps(TOPIC, ensure_ascii=False), encoding="utf-8")
    (folder / "logs/research_notes_draft.md").write_text(RESEARCH, encoding="utf-8")
    (folder / "logs/research_grounding_sources.json").write_text("[]", encoding="utf-8")
    return folder


def test_tripoli_resume_reanchors_then_builds_the_outline_from_the_reanchored_topic(story):
    seen = {}

    def outline(topic, research):
        seen["topic"] = topic
        raise RuntimeError("stop-after-outline")

    fake = gemini_of(verdict(ra.REANCHOR_REQUIRED, ["السجاد"]), (json.dumps(REANCHORED, ensure_ascii=False), []),
                     verdict(ra.PASS))
    with mock.patch.object(generate_daily, "gemini", fake), mock.patch.object(generate_daily, "make_outline", outline):
        with pytest.raises(RuntimeError, match="stop-after-outline"):
            generate_daily.resume_partial_story(story)
    assert seen["topic"]["angle_ar"] == REANCHORED["angle_ar"]
    saved = json.loads((story / "logs/topic.json").read_text(encoding="utf-8"))
    assert saved["research_topic_alignment_status"] == "PASS" and saved["slug"] == TOPIC["slug"]
    history = json.loads((story.parents[1] / "config/topics_history.json").read_text(encoding="utf-8"))
    assert history[0]["angle_ar"] == REANCHORED["angle_ar"] and history[0]["status"] == "GENERATING"
    assert len(history) == 1                                              # no new topic was started


def test_source_evidence_mismatch_cannot_enter_story_generation(story):
    drafted = mock.Mock()
    fake = gemini_of(verdict(ra.RESEARCH_INSUFFICIENT, ["كل العناصر"]), verdict(ra.RESEARCH_INSUFFICIENT))
    with mock.patch.object(generate_daily, "gemini", fake), mock.patch.object(generate_daily, "make_outline") as outline, \
            mock.patch.object(generate_daily, "research_topic", side_effect=RuntimeError("SOURCE_VALIDATION_FAILED: x")), \
            mock.patch.object(generate_daily, "draft_story", drafted):
        with pytest.raises(ra.AlignmentError):
            generate_daily.resume_partial_story(story)
    outline.assert_not_called()
    drafted.assert_not_called()
    assert (story / "logs/research_notes_draft.md").exists() and (story / "logs/topic.json").exists()  # checkpoint intact


def test_reanchoring_discards_an_outline_written_for_the_old_angle(story):
    (story / "logs/outline.json").write_text('{"sections": []}', encoding="utf-8")
    (story / "logs/chapter_01.md").write_text("فصل", encoding="utf-8")
    fake = gemini_of(verdict(ra.REANCHOR_REQUIRED, ["السجاد"]), (json.dumps(REANCHORED, ensure_ascii=False), []),
                     verdict(ra.PASS))
    with mock.patch.object(generate_daily, "gemini", fake):
        generate_daily.ensure_research_alignment(story, dict(TOPIC), RESEARCH, [], story.parents[1] / "config/topics_history.json")
    assert not (story / "logs/outline.json").exists() and not (story / "logs/chapter_01.md").exists()


def test_gate_is_skipped_for_a_finished_story(story):
    (story / "logs/final_story_after_gates.md").write_text("نص", encoding="utf-8")
    (story / "logs/outline.json").write_text('{"sections": []}', encoding="utf-8")
    with mock.patch.object(generate_daily, "ensure_research_alignment") as gate_mock, \
            mock.patch.object(generate_daily, "draft_story", side_effect=RuntimeError("stop")):
        with pytest.raises(RuntimeError, match="stop"):
            generate_daily.resume_partial_story(story)
    gate_mock.assert_not_called()


def test_alignment_failures_are_registered_for_diagnostics():
    import failure_registry as registry
    for code in ("RESEARCH_TOPIC_ALIGNMENT_INSUFFICIENT", "RESEARCH_TOPIC_ALIGNMENT_FAILED"):
        assert registry.REGISTRY[code].stage == "research_alignment"


def test_unsupported_immersion_opportunity_is_replaced_by_reanchor():
    topic_in = dict(TOPIC, immersion_opportunities=["رائحة الزيوت العطرية", "ضوء الميناء"])
    reanchored = dict(REANCHORED, immersion_opportunities=["ضوء الميناء", "أصوات الأسواق"])
    fake = gemini_of(
        verdict(ra.REANCHOR_REQUIRED, ["رائحة الزيوت العطرية"]),
        (json.dumps(reanchored, ensure_ascii=False), []),
        verdict(ra.PASS, supported=reanchored["key_elements"]),
    )
    topic, _, record = gate(fake, topic=topic_in)
    assert record["research_topic_alignment_status"] == "PASS" and len(fake.calls) == 3
    assert topic["immersion_opportunities"] == ["ضوء الميناء", "أصوات الأسواق"]
    assert "رائحة الزيوت العطرية" not in json.dumps(topic["immersion_opportunities"], ensure_ascii=False)
    assert "رائحة الزيوت العطرية" not in fake.calls[2]          # the second check no longer sees the unsupported value


def test_reanchor_without_replacement_immersion_is_not_accepted_when_topic_had_one():
    topic_in = dict(TOPIC, immersion_opportunities=["رائحة الزيوت العطرية"])
    fake = gemini_of(verdict(ra.REANCHOR_REQUIRED, ["رائحة الزيوت العطرية"]),
                     (json.dumps(REANCHORED, ensure_ascii=False), []),
                     (json.dumps(REANCHORED, ensure_ascii=False), []))
    with pytest.raises(ra.AlignmentError):
        gate(fake, topic=topic_in)
