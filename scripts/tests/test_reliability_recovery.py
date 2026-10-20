"""Bounded auto-recovery, durable checkpoints, zombie migration, image/YouTube/review reliability."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

import cloud_pipeline
import episode_selection
import generate_daily
import image_generate
import notify_failure
import restore_failure_checkpoint
import run_summary
import state_store
import youtube_publish


# ---- helpers ------------------------------------------------------------------------------------------

def failing_produce(message: str, *, checkpoint: bool = True, stage: str = "outline"):
    def fake():
        run_summary.start(workflow="generate_daily", action="generate")
        run_summary.stage(stage)
        if checkpoint:
            run_summary.mark_checkpoint("stories/2026-10-18_x", "research_alignment", "outline")
        run_summary.fail(RuntimeError(message))
        raise RuntimeError("Command failed (1); diagnostic log: logs/cloud-generate.log")
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[int] = []
    monkeypatch.setattr(cloud_pipeline.time, "sleep", waited.append)
    return waited


# ---- 8. checkpoint_saved and run diagnostics reflect reality ---------------------------------------

def test_mark_checkpoint_sets_the_full_set_of_fields():
    run_summary.start(workflow="x", action="y")
    run_summary.mark_checkpoint("stories/s", "outline", "chapters")
    summary = run_summary.load()
    assert summary["checkpoint_saved"] is True
    assert (summary["last_completed_stage"], summary["resume_stage"], summary["checkpoint_location"]) == (
        "outline", "chapters", "stories/s")
    assert summary["checkpoint_created_at"] and summary["checkpoint_source"] == "local"


def test_a_saved_topic_counts_as_a_checkpoint_even_if_nothing_marked_it(tmp_path, monkeypatch):
    monkeypatch.setattr(run_summary, "ROOT", tmp_path)
    (tmp_path / "stories" / "s" / "logs").mkdir(parents=True)
    (tmp_path / "stories" / "s" / "logs" / "topic.json").write_text("{}", encoding="utf-8")
    run_summary.start(workflow="x", action="y")
    run_summary.update(story_path="stories/s")
    run_summary.fail(RuntimeError("OUTLINE_GENERATION_FAILED_AFTER_RETRIES: x"))
    summary = run_summary.load()
    assert summary["checkpoint_saved"] is True and summary["checkpoint_location"] == "stories/s"


def test_no_topic_no_checkpoint_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(run_summary, "ROOT", tmp_path)
    run_summary.start(workflow="x", action="y")
    run_summary.update(story_path="stories/missing")
    run_summary.fail(RuntimeError("boom"))
    assert run_summary.load()["checkpoint_saved"] is False


def test_resume_marks_the_checkpoint_before_any_stage_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)
    story = tmp_path / "stories" / "2026-10-18_s"
    (story / "logs").mkdir(parents=True)
    (story / "logs/topic.json").write_text(json.dumps({"slug": "s", "topic_ar": "ت", "angle_ar": "ز"}), encoding="utf-8")
    (story / "logs/research_notes_draft.md").write_text("بحث", encoding="utf-8")
    with mock.patch.object(generate_daily, "ensure_research_alignment", side_effect=RuntimeError("stop")):
        with pytest.raises(RuntimeError, match="stop"):
            generate_daily.resume_partial_story(story)
    assert run_summary.load()["checkpoint_saved"] is True


def test_durable_checkpoint_copies_to_automation_state_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)
    story = tmp_path / "stories" / "s"
    story.mkdir(parents=True)
    with mock.patch.object(state_store, "save_story_checkpoint") as save:
        monkeypatch.delenv("SLEEP_HISTORY_DURABLE_CHECKPOINTS", raising=False)
        generate_daily.durable_checkpoint(story, "outline", "chapters")
        save.assert_not_called()
        monkeypatch.setenv("SLEEP_HISTORY_DURABLE_CHECKPOINTS", "1")
        generate_daily.durable_checkpoint(story, "outline", "chapters")
        save.assert_called_once_with(story)
    assert run_summary.load()["last_completed_stage"] == "outline"


def test_a_failing_durable_save_never_fails_production(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)
    monkeypatch.setenv("SLEEP_HISTORY_DURABLE_CHECKPOINTS", "1")
    with mock.patch.object(state_store, "save_story_checkpoint", side_effect=RuntimeError("git down")):
        generate_daily.durable_checkpoint(tmp_path / "stories" / "s", "outline", "chapters")


# ---- 9/10. bounded automatic recovery ----------------------------------------------------------------

def test_auto_recoverable_failure_is_retried_inside_the_job_from_the_checkpoint(monkeypatch, sleeps):
    first = failing_produce("OUTLINE_GENERATION_FAILED_AFTER_RETRIES: x")
    calls = []

    def sequence():
        calls.append(1)
        if len(calls) == 1:
            return first()
        return {"ok": True}
    monkeypatch.setattr(cloud_pipeline, "produce", sequence)
    assert cloud_pipeline.produce_with_recovery() == {"ok": True}
    assert len(calls) == 2 and sleeps == [30]
    summary = run_summary.load()
    assert summary["auto_recovery_attempted"] is True and len(summary["auto_recovery_log"]) == 1
    assert summary["auto_recovery_log"][0]["error_code"] == "OUTLINE_GENERATION_FAILED_AFTER_RETRIES"
    assert summary["checkpoint_saved"] is True            # the same checkpoint is reused, not rebuilt
    run_summary.finish()
    assert run_summary.load()["status"] == "success"


@pytest.mark.parametrize("message", [
    "OUTLINE_GENERATION_FAILED_AFTER_RETRIES: x",     # validation, limit 2
    "CLOUDFLARE_NETWORK_ERROR: timeout",              # transient, capped by the job-wide limit
])
def test_recovery_never_loops_forever(monkeypatch, sleeps, message):
    fake = mock.Mock(side_effect=failing_produce(message))
    monkeypatch.setattr(cloud_pipeline, "produce", fake)
    with pytest.raises(RuntimeError):
        cloud_pipeline.produce_with_recovery()
    assert fake.call_count == 1 + cloud_pipeline.MAX_AUTORECOVERY_ATTEMPTS
    assert sleeps == [30, 90]
    summary = run_summary.load()
    assert summary["status"] == "failed" and len(summary["auto_recovery_log"]) == 2
    assert "🔁" in notify_failure.build_message("https://run")


@pytest.mark.parametrize("message, checkpoint", [
    ("TTS_WAITING_FOR_QUOTA: Gemini quota used up", True),         # a wait, not a retry
    ("WAITING_FOR_LLM_QUOTA: all blocked", True),
    ("BLOCKED_COST: unapproved LLM models", True),                  # cost block
    ("IMAGE_FREE_CAPACITY_EXHAUSTED: Pollinations HTTP 402", True), # free capacity gone: no immediate retry
    ("BYTEZ_API_KEY is missing from the external secrets store", True),   # owner action
    ("invalid_grant: expired", True),                               # owner action
    ("boom", True),                                                 # unknown = code defect
    ("TTS_CHUNKING_FAILED: sentence exceeds 5600 characters", True),  # code defect
    ("STORY_QUALITY_GATE_REJECTED: {\"deterministic\":[\"curiosity does not decay\"]}", True),
    ("OUTLINE_GENERATION_FAILED_AFTER_RETRIES: x", False),          # nothing saved yet at a late stage
])
def test_only_safe_failures_are_retried(monkeypatch, sleeps, message, checkpoint):
    fake = mock.Mock(side_effect=failing_produce(message, checkpoint=checkpoint))
    monkeypatch.setattr(cloud_pipeline, "produce", fake)
    with pytest.raises(RuntimeError):
        cloud_pipeline.produce_with_recovery()
    assert fake.call_count == 1 and sleeps == []


def test_topic_discovery_can_retry_without_a_checkpoint(monkeypatch, sleeps):
    outcomes = [failing_produce("TOPIC_DISCOVERY_FAILED: fewer than 4 valid candidates", checkpoint=False,
                                stage="topic_selection"), lambda: {"ok": True}]
    monkeypatch.setattr(cloud_pipeline, "produce", lambda: outcomes.pop(0)())
    assert cloud_pipeline.produce_with_recovery() == {"ok": True}


def test_recovery_stops_late_in_the_job():
    summary = {"status": "failed", "recovery_policy": "AUTO_RECOVERABLE", "immediate_retry": True,
               "checkpoint_saved": True, "failed_stage": "outline", "max_attempts": 2}
    assert cloud_pipeline.recovery_decision(summary, 0, 10)[0] is True
    assert cloud_pipeline.recovery_decision(summary, 0, cloud_pipeline.AUTORECOVERY_DEADLINE_SECONDS + 1)[0] is False
    assert cloud_pipeline.recovery_decision(summary, 2, 10)[0] is False


def test_the_same_error_code_is_not_retried_past_its_limit():
    log = [{"error_code": "X"}, {"error_code": "X"}]
    summary = {"status": "failed", "recovery_policy": "AUTO_RECOVERABLE", "immediate_retry": True,
               "checkpoint_saved": True, "failed_stage": "outline", "error_code": "X",
               "max_attempts": 3, "auto_recovery_log": log}
    assert cloud_pipeline.recovery_decision(summary, 1, 0)[0] is False


# ---- resume-only scheduled passes ---------------------------------------------------------------------

@pytest.fixture
def prod(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(cloud_pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(cloud_pipeline, "SKIP_MARKER", tmp_path / ".runtime" / "produce_skipped.json")
    monkeypatch.setattr(cloud_pipeline.state_store, "_load_state", lambda: {})
    return tmp_path


def write_history(root: Path, records: list[dict]) -> None:
    (root / "config" / "topics_history.json").write_text(json.dumps(records), encoding="utf-8")


def test_resume_only_without_an_unfinished_episode_does_nothing_and_never_starts_a_topic(prod, monkeypatch):
    write_history(prod, [{"slug": "done", "status": "PUBLISHED", "story_path": "stories/done"}])
    start = mock.Mock()
    monkeypatch.setattr(cloud_pipeline, "produce_with_recovery", start)
    result = cloud_pipeline.resume_only()
    assert result["skipped"] == "no unfinished episode"
    start.assert_not_called()
    assert cloud_pipeline.notify_saved_review()["skipped"]            # the review step is a no-op too


def test_resume_only_waits_for_a_future_quota_window_and_resumes_after_it(prod, monkeypatch):
    story = prod / "stories" / "2026-10-18_t"
    (story / "logs").mkdir(parents=True)
    (story / "logs/topic.json").write_text("{}", encoding="utf-8")
    now = episode_selection.dt.datetime.now(episode_selection.dt.timezone.utc)
    record = {"slug": "t", "status": "WAITING_FOR_LLM_QUOTA", "story_path": "stories/2026-10-18_t",
              "created_at": now.isoformat(), "blocked_until": (now + episode_selection.dt.timedelta(hours=3)).isoformat()}
    write_history(prod, [record])
    resume = mock.Mock(return_value={"ok": True})
    monkeypatch.setattr(cloud_pipeline, "produce_with_recovery", resume)
    assert "waiting for quota" in cloud_pipeline.resume_only()["skipped"]
    resume.assert_not_called()
    record["blocked_until"] = (now - episode_selection.dt.timedelta(minutes=1)).isoformat()
    write_history(prod, [record])
    cloud_pipeline.SKIP_MARKER.unlink(missing_ok=True)
    assert cloud_pipeline.resume_only() == {"ok": True}
    resume.assert_called_once()


# ---- 11. durable state: an expired or missing artifact is not the end ---------------------------------

def make_story(root: Path) -> Path:
    story = root / "stories" / "2026-10-18_tripoli"
    (story / "logs").mkdir(parents=True)
    for name, text in {"topic.json": '{"slug":"tripoli"}', "research_notes_draft.md": "بحث", "outline.json": "{}",
                       "chapter_01.md": "فصل", "cloud.log": "ignored"}.items():
        (story / "logs" / name).write_text(text, encoding="utf-8")
    (story / "logs" / "huge.json").write_text("x" * (state_store.CHECKPOINT_MAX_FILE_BYTES + 1), encoding="utf-8")
    (story / "audio_chunks").mkdir()
    (story / "audio_chunks" / "001.mp3").write_bytes(b"audio")
    (story / "14_video_final.mp4").write_bytes(b"video")
    return story


def test_checkpoint_bundle_holds_small_text_state_only(tmp_path):
    bundle = state_store.collect_story_checkpoint(make_story(tmp_path))
    assert set(bundle["files"]) == {"logs/topic.json", "logs/research_notes_draft.md", "logs/outline.json",
                                    "logs/chapter_01.md"}
    assert state_store.collect_story_checkpoint(tmp_path / "stories") is None


def test_checkpoint_round_trip_restores_missing_files_and_never_overwrites_local_work(tmp_path):
    source = tmp_path / "a"
    bundle = state_store.collect_story_checkpoint(make_story(source))
    bundle["files"]["../../evil.json"] = "{}"
    bundle["files"]["logs/../x.json"] = "{}"
    bundle["files"]["logs/run.exe"] = "x"
    target = tmp_path / "b"
    (target / "stories" / "2026-10-18_tripoli" / "logs").mkdir(parents=True)
    local = target / "stories" / "2026-10-18_tripoli" / "logs" / "outline.json"
    local.write_text('{"newer": true}', encoding="utf-8")
    with mock.patch.object(state_store, "_read_remote", return_value=json.dumps(bundle)):
        restored = state_store.restore_story_checkpoint(target, "2026-10-18_tripoli")
    assert restored == 3
    assert local.read_text(encoding="utf-8") == '{"newer": true}'
    assert not (target / "evil.json").exists() and not (target / "stories" / "x.json").exists()
    assert not list(target.rglob("run.exe"))


def test_save_story_checkpoint_writes_one_bundle_file_to_automation_state(tmp_path):
    story = make_story(tmp_path)
    captured = {}

    def fake_update(mutator, message):
        captured.update(mutator(lambda name: None))
        return True
    with mock.patch.object(state_store, "_update_remote", fake_update):
        assert state_store.save_story_checkpoint(story)
    name = f"checkpoint--{story.name}.json"
    assert list(captured) == [name] and "/" not in name
    assert json.loads(captured[name])["story"] == story.name


def test_expired_artifacts_fall_back_to_the_durable_checkpoint(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "topics_history.json").write_text(json.dumps([{
        "slug": "tripoli", "status": "GENERATING", "story_path": "stories/2026-10-18_tripoli",
        "created_at": episode_selection.dt.datetime.now(episode_selection.dt.timezone.utc).isoformat()}]), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(restore_failure_checkpoint, "github_json", lambda *_: {"artifacts": [
        {"name": "sleep-history-failure-1", "expired": True, "created_at": "2026-09-01", "id": 1}]})
    with mock.patch.object(state_store, "restore_story_checkpoint", return_value=4) as durable, \
            mock.patch.object(state_store, "_load_state", return_value={}):
        assert restore_failure_checkpoint.restore_latest_checkpoint(tmp_path) is True
    durable.assert_called_once_with(tmp_path, "2026-10-18_tripoli")
    summary = run_summary.load()
    assert summary["restored_from_checkpoint"] is True and summary["checkpoint_source"] == "automation-state"


def test_failure_artifacts_are_kept_30_days():
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/production.yml").read_text(encoding="utf-8")
    assert "retention-days: 30" in text and "retention-days: 7" not in text


# ---- 12. zombie state ---------------------------------------------------------------------------------

DHOFAR = {"slug": "dhofar-incense-network", "topic": "ظفار", "status": "pending_approval",
          "story_path": "stories/2026-10-07_dhofar-incense-network"}


def test_zombie_record_is_retired_once_with_an_audit_trail(tmp_path, capsys):
    history = [dict(DHOFAR), {"slug": "live", "status": "GENERATING", "story_path": "stories/live",
                              "created_at": episode_selection.dt.datetime.now(episode_selection.dt.timezone.utc).isoformat()}]
    assert episode_selection.migrate_zombie_records(history, tmp_path) == ["dhofar-incense-network"]
    assert history[0]["status"] == "ABANDONED_LEGACY" and history[0]["status_before_migration"] == "pending_approval"
    assert history[0]["migrated_at"] and history[0]["migration_reason"] and history[1]["status"] == "GENERATING"
    assert episode_selection.migrate_zombie_records(history, tmp_path) == []        # idempotent
    capsys.readouterr()
    assert episode_selection.active_episode(history, tmp_path)["slug"] == "live"
    assert "zombie" not in capsys.readouterr().err.lower()                            # no warning on later runs


def test_records_with_a_folder_an_archive_or_a_recent_date_are_never_migrated(tmp_path):
    (tmp_path / "stories" / "has-folder" / "logs").mkdir(parents=True)
    (tmp_path / "stories" / "has-folder" / "logs" / "topic.json").write_text("{}", encoding="utf-8")
    recent = episode_selection.dt.datetime.now(episode_selection.dt.timezone.utc).isoformat()
    history = [
        {"slug": "a", "status": "GENERATING", "story_path": "stories/has-folder"},
        {"slug": "b", "status": "pending_approval", "story_path": "stories/gone", "archive_identifier": "x"},
        {"slug": "c", "status": "GENERATING", "story_path": "stories/gone", "created_at": recent},
        {"slug": "d", "status": "PUBLISHED", "story_path": "stories/gone"},
    ]
    assert episode_selection.migrate_zombie_records(history, tmp_path) == []
    assert [r["status"] for r in history] == ["GENERATING", "pending_approval", "GENERATING", "PUBLISHED"]


def test_restore_step_persists_the_migration_to_the_history_file(tmp_path):
    (tmp_path / "config").mkdir()
    path = tmp_path / "config" / "topics_history.json"
    path.write_text(json.dumps([dict(DHOFAR)]), encoding="utf-8")
    assert restore_failure_checkpoint.migrate_history(tmp_path) == ["dhofar-incense-network"]
    assert json.loads(path.read_text(encoding="utf-8"))[0]["status"] == "ABANDONED_LEGACY"
    assert restore_failure_checkpoint.migrate_history(tmp_path) == []


# ---- 13. image retries target the real rejection reason ---------------------------------------------

def gate_error(**flags) -> RuntimeError:
    return RuntimeError("IMAGE_ACCURACY_GATE_FAILED: " + json.dumps({"status": "FAIL", **flags}))


def test_text_or_watermark_rejection_tightens_the_no_text_rule_specifically():
    lines = image_generate.corrective_lines(image_generate.rejection_gate(gate_error(no_text_or_watermark=False)))
    assert lines == [image_generate.NO_TEXT_EXCLUSION]
    for word in ("letters", "words", "signs", "logos", "glyph"):
        assert word in image_generate.NO_TEXT_EXCLUSION


def test_each_rejection_reason_gets_its_own_instruction():
    gate = image_generate.rejection_gate
    assert image_generate.corrective_lines(gate(gate_error(no_modern_objects=False))) == [image_generate.MODERN_EXCLUSION]
    assert image_generate.corrective_lines(gate(gate_error(historically_plausible=False))) == [image_generate.PERIOD_EXCLUSION]
    assert image_generate.corrective_lines(gate(gate_error(topic_match=False))) == [image_generate.TOPIC_MATCH_REQUIREMENT]
    assert len(image_generate.corrective_lines(gate(gate_error()))) == 2          # a bare FAIL tightens both common causes
    assert image_generate.rejection_gate(RuntimeError("IMAGE_ACCURACY_GATE_FAILED: URLError")) == {}


def test_corrective_prompt_stays_under_the_provider_limit_and_never_duplicates():
    long_prompt = "A paper workshop. " * 100
    tightened = image_generate.tighten_prompt(long_prompt, {"no_text_or_watermark": False, "no_modern_objects": False,
                                                            "historically_plausible": False, "topic_match": False})
    assert len(tightened) <= 2048
    again = image_generate.tighten_prompt(tightened, {"no_text_or_watermark": False})
    assert again.count(image_generate.NO_TEXT_EXCLUSION) == 1


def test_retry_loop_applies_the_targeted_prompt_logs_each_attempt_and_stays_on_the_image_stage(tmp_path):
    story = tmp_path
    (story / "08_image_prompt_en.txt").write_text("A paper workshop in Samarkand", encoding="utf-8")
    (story / "15_metadata.json").write_text("{}", encoding="utf-8")
    prompts = []

    def fake_generate(path, seed):
        prompts.append((path / "08_image_prompt_en.txt").read_text(encoding="utf-8"))
        image_generate.LAST_ATTEMPT.update(provider="cloudflare_workers_ai", model="flux-1-schnell")
        if len(prompts) == 1:
            raise gate_error(no_text_or_watermark=False, issues=["Arabic-like letters on the wall"])
        if len(prompts) == 2:
            raise gate_error(no_modern_objects=False)

    with mock.patch.dict(image_generate.SETTINGS, {"image_accuracy_retry_attempts": 3}), \
            mock.patch.object(image_generate, "generate", fake_generate):
        image_generate.generate_with_accuracy_retries(story, seed=5)
    assert image_generate.NO_TEXT_EXCLUSION not in prompts[0]
    assert image_generate.NO_TEXT_EXCLUSION in prompts[1] and image_generate.MODERN_EXCLUSION not in prompts[1]
    assert image_generate.MODERN_EXCLUSION in prompts[2] and image_generate.NO_TEXT_EXCLUSION in prompts[2]
    attempts = json.loads((story / "logs/image_attempts.json").read_text(encoding="utf-8"))
    assert [a["outcome"] for a in attempts] == ["quality_rejection", "quality_rejection", "passed"]
    assert all(a["provider"] == "cloudflare_workers_ai" and a["model"] == "flux-1-schnell" for a in attempts)
    assert json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))["image_attempts"] == attempts


def test_free_capacity_exhaustion_is_not_retried_and_is_saved_for_a_later_run(tmp_path):
    (tmp_path / "08_image_prompt_en.txt").write_text("p", encoding="utf-8")
    boom = RuntimeError("IMAGE_FREE_CAPACITY_EXHAUSTED: BLOCKED_COST_OR_FREE_LIMIT: Pollinations (HTTP 402)")
    with mock.patch.dict(image_generate.SETTINGS, {"image_accuracy_retry_attempts": 4}), \
            mock.patch.object(image_generate, "generate", side_effect=boom) as generate:
        with pytest.raises(RuntimeError, match="IMAGE_FREE_CAPACITY_EXHAUSTED"):
            image_generate.generate_with_accuracy_retries(tmp_path)
    assert generate.call_count == 1
    run_summary.start(workflow="x", action="y")
    run_summary.fail(boom)
    summary = run_summary.load()
    assert summary["cost_blocked"] is True and summary["recovery_policy"] == "AUTO_RECOVERABLE"
    assert summary["immediate_retry"] is False                       # a later scheduled pass resumes it


# ---- 14/15. YouTube duplicate protection --------------------------------------------------------------

READ = {youtube_publish.SCOPE_MANAGE}
UPLOAD_ONLY = {youtube_publish.SCOPE_UPLOAD}
GOOD_PACKAGING = json.dumps({
    "youtube_packaging_status": "passed", "final_youtube_title": "title", "title": "title",
    "thumbnail_text": "سر الورق", "youtube_packaging_validation_status": "passed",
    "thumbnail_text_render_status": "passed"})


def youtube_fixture(*, scopes, metadata, search_items=None, thumbnail_error=None):
    youtube = mock.MagicMock()
    insert = mock.MagicMock()
    insert.next_chunk.return_value = (None, {"id": "NEW123"})
    youtube.videos.return_value.insert.return_value = insert
    youtube.videos.return_value.list.return_value.execute.return_value = {
        "items": [{"status": {"uploadStatus": "processed", "privacyStatus": "public"}}]}
    youtube.search.return_value.list.return_value.execute.return_value = {"items": search_items or []}
    if thumbnail_error:
        youtube.thumbnails.return_value.set.return_value.execute.side_effect = thumbnail_error
    patches = [
        mock.patch.object(youtube_publish, "archive_metadata", return_value={"metadata": metadata}),
        mock.patch.object(youtube_publish, "build", return_value=youtube),
        mock.patch.object(youtube_publish, "credentials", return_value=mock.MagicMock()),
        mock.patch.object(youtube_publish, "get_granted_scopes", return_value=scopes),
        mock.patch.object(youtube_publish, "publish_mode", return_value="approved_direct_public"),
        mock.patch.object(youtube_publish, "record_remote_upload"),
        mock.patch.object(youtube_publish, "download", side_effect=lambda i, f, path: path.write_bytes(b"X" * 2000)),
        mock.patch.object(youtube_publish, "read_text", side_effect=lambda i, f: {
            "01_title.txt": "title", "02_description.txt": "desc", "15_metadata.json": GOOD_PACKAGING}[f]),
        mock.patch.object(youtube_publish, "MediaFileUpload"),
        mock.patch.object(youtube_publish.time, "sleep"),
    ]
    return youtube, insert, patches


@pytest.fixture(autouse=True)
def isolated_pending_record(tmp_path, monkeypatch):
    """The pending-video record normally lives in the repository's .runtime: never touch it from tests."""
    monkeypatch.setattr(youtube_publish, "pending_youtube_record_path", lambda ident: tmp_path / f"pending_{ident}.json")


def run_publish(patches, recorded=None):
    sink = recorded if recorded is not None else []
    patches = patches + [mock.patch.object(youtube_publish, "_record_remote_fields",
                                           side_effect=lambda ident, fields: sink.append(dict(fields)))]
    for patch in patches:
        patch.start()
    try:
        return youtube_publish.publish_archive_item("item-1")
    finally:
        mock.patch.stopall()


@pytest.mark.parametrize("where", ["metadata", "pending", "history", "state"])
def test_an_existing_video_id_in_any_store_prevents_a_second_upload(tmp_path, monkeypatch, where):
    monkeypatch.setattr(youtube_publish, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / ".runtime").mkdir()
    metadata = {}
    if where == "metadata":
        metadata["youtube_video_id"] = "OLD1"
    elif where == "pending":
        youtube_publish.save_pending_youtube_id("item-1", "OLD1")
    elif where == "history":
        (tmp_path / "config" / "topics_history.json").write_text(
            json.dumps([{"archive_identifier": "item-1", "youtube_video_id": "OLD1"}]), encoding="utf-8")
    state = {"episodes": {"e": {"archive_identifier": "item-1", "youtube_video_id": "OLD1"}}} if where == "state" else {}
    with mock.patch("state_store._load_state", return_value=state):
        youtube, insert, patches = youtube_fixture(scopes=READ, metadata=metadata)
        result = run_publish(patches)
    assert result["video_id"] == "OLD1"
    youtube.videos.return_value.insert.assert_not_called()


def test_thumbnail_failure_after_upload_keeps_the_video_and_never_creates_another(monkeypatch):
    from googleapiclient.errors import HttpError
    error = HttpError(mock.Mock(status=500, reason="boom"), b"{}")
    recorded: list[dict] = []
    youtube, insert, patches = youtube_fixture(scopes=READ, metadata={}, thumbnail_error=error)
    with mock.patch("error_classifier.time.sleep"):
        first = run_publish(patches, recorded)
    assert first["video_id"] == "NEW123" and insert.next_chunk.call_count == 1
    assert {"youtube_thumbnail_status": "failed"} in recorded
    # Retry run: the video id is now known; no insert, and the missing thumbnail is retried.
    recorded.clear()
    youtube2, insert2, patches2 = youtube_fixture(
        scopes=READ, metadata={"youtube_video_id": "NEW123", "youtube_thumbnail_status": "failed"})
    second = run_publish(patches2, recorded)
    youtube2.videos.return_value.insert.assert_not_called()
    youtube2.thumbnails.return_value.set.assert_called_once()
    assert second["video_id"] == "NEW123" and {"youtube_thumbnail_status": "set"} in recorded


def test_intent_is_recorded_before_insert_and_a_crash_without_read_scope_does_not_duplicate(tmp_path):
    order: list[str] = []
    youtube, insert, patches = youtube_fixture(scopes=UPLOAD_ONLY, metadata={})
    insert.next_chunk.side_effect = lambda: order.append("insert") or (None, {"id": "NEW123"})
    patches.append(mock.patch.object(youtube_publish, "record_upload_intent", side_effect=lambda i, s: order.append(f"intent:{s}")))
    run_publish(patches)
    assert order[:2] == ["intent:inserting", "insert"]
    # The next run after a runner crash: the video id was never recorded anywhere, only the intent marker.
    (tmp_path / "pending_item-1.json").unlink(missing_ok=True)
    youtube2, insert2, patches2 = youtube_fixture(scopes=UPLOAD_ONLY, metadata={"youtube_upload_status": "inserting"})
    blocked = run_publish(patches2)
    assert blocked["requires_owner_action"] and blocked["action"] == "REAUTHORIZE_YOUTUBE_OAUTH_WITH_READ_SCOPE"
    youtube2.videos.return_value.insert.assert_not_called()


def test_with_a_read_scope_the_marker_is_resolved_by_search_and_a_clean_miss_uploads_once():
    found = [{"id": {"videoId": "OLD9"}, "snippet": {"title": "title"}}]
    youtube, insert, patches = youtube_fixture(scopes=READ, metadata={"youtube_upload_status": "inserting"}, search_items=found)
    assert run_publish(patches)["video_id"] == "OLD9"
    youtube.videos.return_value.insert.assert_not_called()
    youtube2, insert2, patches2 = youtube_fixture(scopes=READ, metadata={"youtube_upload_status": "inserting"})
    assert run_publish(patches2)["video_id"] == "NEW123" and insert2.next_chunk.call_count == 1


def test_a_clean_4xx_rejection_clears_the_intent_marker():
    from googleapiclient.errors import HttpError
    youtube, insert, patches = youtube_fixture(scopes=READ, metadata={})
    insert.next_chunk.side_effect = HttpError(mock.Mock(status=403, reason="quotaExceeded"), b"{}")
    intents = []
    patches.append(mock.patch.object(youtube_publish, "record_upload_intent", side_effect=lambda i, s: intents.append(s)))
    with pytest.raises(HttpError):
        run_publish(patches)
    assert intents == ["inserting", "rejected"]


# ---- 16. review workflow reliability ----------------------------------------------------------------

@pytest.mark.parametrize("command, message, code", [
    ("publish", "Internet Archive upload failed with HTTP 503", "ARCHIVE_UPLOAD_FAILED"),
    ("publish", "invalid_grant: Token has been expired or revoked", "YOUTUBE_AUTH_FAILED"),
    ("regenerate", "IMAGE_ACCURACY_GATE_FAILED: {}", "IMAGE_ACCURACY_GATE_FAILED"),
])
def test_review_command_failures_keep_the_original_cause_for_telegram(monkeypatch, command, message, code):
    monkeypatch.setattr(sys, "argv", ["cloud_pipeline.py", command, "--identifier", "item-1"])
    with mock.patch.object(cloud_pipeline, "dispatch_command", side_effect=RuntimeError(message)), \
            mock.patch.object(cloud_pipeline.state_store, "ensure_hydrated"):
        with pytest.raises(RuntimeError):
            cloud_pipeline.main()
    summary = run_summary.load()
    assert summary["action"] == command and summary["error_code"] == code and summary["status"] == "failed"
    text = notify_failure.build_message("https://run")
    assert code in text and "https://run" in text


# ---- 18. the current Tripoli recovery path ---------------------------------------------------------

TRIPOLI = {
    "slug": "tripoli-lybia-karamanli-daily-life", "topic_ar": "الحياة اليومية في طرابلس الغرب خلال العصر القره مانلي",
    "angle_ar": "عائلة تحضّر الصوف وتصبغه وتنسج السجاد", "period_ar": "القرن الثامن عشر", "location_ar": "طرابلس",
    "story_format": "CRAFT_QUEST", "narrative_follow": "عائلة تنسج الصوف", "soft_narrative_spine": "صبغ الخيوط",
    "gentle_curiosity": "كيف صُبغت الخيوط؟", "key_elements": ["السجاد", "الصوف", "الزيوت العطرية"],
}


def section(i: int, scenes: int = 2, words: int = 750) -> dict:
    phases = ["CALM_HOOK", "EXPLORATION", "EXPLORATION", "SETTLE", "SETTLE", "UNWIND", "SOFT_FADE"]
    states = ["introduced", "in_progress", "in_progress", "nearly_solved", "nearly_solved", "resolved", "aftermath"]
    opened = [1, 1, 0, 0, 0, 0, 0]
    return {"title": f"قسم {i}", "phase": phases[i], "brief": "ب", "target_words": words,
            "quest_beat": f"خطوة {i}", "quest_state": states[i], "new_questions_opened": opened[i],
            "dialogue_level": "none" if i >= 5 else "light", "named_characters": ["يوسف"], "scenes": [
        {"place_or_anchor": f"م{i}{n}", "action": "نقترب", "main_idea": f"ف{i}{n}", "gentle_question": "؟",
         "transition": "ثم"} for n in range(scenes)]}


def reply(sections) -> tuple[str, list]:
    return json.dumps({"sections": sections}, ensure_ascii=False), []


def test_tripoli_resumes_the_same_episode_reanchors_repairs_the_outline_and_reaches_chapters(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    folder = tmp_path / "stories" / "2026-10-18_tripoli-lybia-karamanli-daily-life"
    (folder / "logs").mkdir(parents=True)
    (folder / "logs/topic.json").write_text(json.dumps(TRIPOLI, ensure_ascii=False), encoding="utf-8")
    (folder / "logs/research_notes_draft.md").write_text("بحث\n## خريطة الادعاءات", encoding="utf-8")
    (folder / "logs/research_grounding_sources.json").write_text("[]", encoding="utf-8")
    history = [{"slug": TRIPOLI["slug"], "topic": TRIPOLI["topic_ar"], "angle": TRIPOLI["angle_ar"],
                "status": "GENERATING", "story_path": folder.relative_to(tmp_path).as_posix(),
                "created_at": episode_selection.dt.datetime.now(episode_selection.dt.timezone.utc).isoformat()}]
    (tmp_path / "config/topics_history.json").write_text(json.dumps(history), encoding="utf-8")
    anchored = {"angle_ar": "ضوء النهار على الميناء", "narrative_follow": "حركة الميناء", "soft_narrative_spine": "تدرج الضوء",
                "gentle_curiosity": "كيف خدم الميناء المدينة؟", "key_elements": ["الميناء", "البيوت البيضاء"]}
    verdict = lambda status, unsupported=(): (json.dumps(  # noqa: E731
        {"status": status, "supported_elements": ["الميناء"], "unsupported_elements": list(unsupported), "reason": "r"},
        ensure_ascii=False), [])
    wrong_scenes = [section(i) for i in range(7)]
    wrong_scenes[3]["scenes"] = wrong_scenes[3]["scenes"][:1]                    # Run 65, attempt 1
    wrong_count = [section(i) for i in range(6)]                                  # attempt 2: six sections
    total_5250 = [section(i) for i in range(7)]                                   # attempt 3: 7 x 750 = 5250
    replies = iter([
        verdict("REANCHOR_REQUIRED", ["السجاد"]), (json.dumps(anchored, ensure_ascii=False), []), verdict("PASS"),
        reply(wrong_scenes), reply(wrong_count), reply(total_5250),
    ])
    stages: list[tuple[str, str | None]] = []
    monkeypatch.setattr(generate_daily, "durable_checkpoint", lambda story, done, nxt: stages.append((done, nxt)))

    class Reached(Exception):
        pass

    with mock.patch.object(generate_daily, "gemini", side_effect=lambda *a, **k: next(replies)) as gem, \
            mock.patch.object(generate_daily, "choose_topic") as choose, \
            mock.patch.object(generate_daily, "choose_valid_topic") as choose_valid, \
            mock.patch.object(generate_daily, "research_topic") as research, \
            mock.patch.object(generate_daily, "draft_story", side_effect=Reached):
        with pytest.raises(Reached):
            generate_daily.resume_partial_story(folder)
    choose.assert_not_called(); choose_valid.assert_not_called(); research.assert_not_called()   # same episode, same research
    assert gem.call_count == 6
    saved_topic = json.loads((folder / "logs/topic.json").read_text(encoding="utf-8"))
    assert saved_topic["slug"] == TRIPOLI["slug"] and saved_topic["angle_ar"] == anchored["angle_ar"]
    assert saved_topic["research_topic_alignment_status"] == "PASS"
    outline = json.loads((folder / "logs/outline.json").read_text(encoding="utf-8"))["sections"]
    assert sum(s["target_words"] for s in outline) >= 5500
    assert stages == [("research_alignment", "outline"), ("outline", "chapters")]
    saved_history = json.loads((tmp_path / "config/topics_history.json").read_text(encoding="utf-8"))
    assert len(saved_history) == 1 and saved_history[0]["angle_ar"] == anchored["angle_ar"]
