import json
from unittest.mock import patch

import pytest
import generate_daily as gd
import outline_json
from test_outline_retry import TOPIC, outline, reply, MALFORMED


def test_trailing_commas_repaired_without_changing_strings_or_calling_model_twice(tmp_path):
    sections = outline()
    sections[0]["title"] = 'فاصلة ,} واقتباس " داخل النص'
    raw = json.dumps({"sections": sections}, ensure_ascii=False)
    raw = "\ufeff```json\n" + raw[:-1] + ",}\n```"
    with patch.object(gd, "gemini", return_value=(raw, [])) as model:
        result = gd.make_outline(TOPIC, "بحث", checkpoint_dir=tmp_path / "logs")
    assert result == sections
    assert model.call_count == 1


@pytest.mark.parametrize("text", ['{"x":', '{"x":"unfinished', '{"x":1}{"x":2}',
                                  '{"x":NaN}', '{"x":1,"x":2}'])
def test_ambiguous_or_truncated_json_never_invented(text):
    with pytest.raises(ValueError):
        outline_json.repair_json_syntax(text)


def test_validated_response_survives_crash_before_outline_file_is_written(tmp_path):
    sections = outline()
    logs = tmp_path / "logs"
    with patch.object(gd, "gemini", return_value=reply(sections)) as model:
        assert gd.make_outline(TOPIC, "بحث", checkpoint_dir=logs) == sections
    assert model.call_count == 1
    assert not (logs / "outline.json").exists()
    with patch.object(gd, "gemini", side_effect=AssertionError("MUST_NOT_REGENERATE")):
        assert gd.make_outline(TOPIC, "بحث", checkpoint_dir=logs) == sections


def test_exhausted_responses_kept_without_corrupting_topic_or_research(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    topic = logs / "topic.json"
    topic.write_text(json.dumps(TOPIC), encoding="utf-8")
    research = logs / "research_notes_draft.md"
    research.write_text("saved research", encoding="utf-8")
    before = [topic.read_bytes(), research.read_bytes()]
    with patch.object(gd, "gemini", return_value=MALFORMED) as model:
        with pytest.raises(RuntimeError, match="OUTLINE_GENERATION_FAILED_AFTER_RETRIES"):
            gd.make_outline(TOPIC, "بحث", checkpoint_dir=logs)
    saved = json.loads((logs / "outline_checkpoint.json").read_text(encoding="utf-8"))
    assert saved["status"] == "invalid" and saved["raw_response"] == MALFORMED[0]
    assert saved["attempt"] == model.call_count == gd.OUTLINE_MAX_MODEL_CALLS == 2
    assert before == [topic.read_bytes(), research.read_bytes()]
    assert not (logs / "outline.json").exists()


def test_changed_research_does_not_reuse_old_checkpoint(tmp_path):
    with patch.object(gd, "gemini", return_value=reply(outline())):
        gd.make_outline(TOPIC, "research one", checkpoint_dir=tmp_path)
    newer = outline()
    with patch.object(gd, "gemini", return_value=reply(newer)) as model:
        assert gd.make_outline(TOPIC, "research two", checkpoint_dir=tmp_path) == newer
    assert model.call_count == 1


def test_existing_trailing_commentary_tolerance_is_preserved():
    sections = outline()
    raw = json.dumps({"sections": sections}) + "\nتم التحقق من المخطط."
    with patch.object(gd, "gemini", return_value=(raw, [])) as model:
        assert gd.make_outline(TOPIC, "بحث") == sections
    assert model.call_count == 1


@pytest.mark.parametrize("status", ["MEDIA_READY", "REVIEW_PENDING", "APPROVED_FOR_PUBLISH", "PUBLISHING", "PUBLISHED"])
def test_completed_episode_is_never_regenerated(tmp_path, status):
    metadata = tmp_path / "15_metadata.json"
    metadata.write_text(json.dumps({"approval_status": status}), encoding="utf-8")
    before = metadata.read_bytes()
    with patch.object(gd, "gemini", side_effect=AssertionError("NO_AI")), \
         patch.object(gd, "draft_story", side_effect=AssertionError("NO_REGEN")):
        assert gd.resume_partial_story(tmp_path) == tmp_path
    assert metadata.read_bytes() == before
