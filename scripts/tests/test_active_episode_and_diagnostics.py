"""Active-episode gate, planned quota pause, structured run summary and Telegram messages."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

import cloud_pipeline
import episode_selection
import generate_daily
import llm_router
import notify_failure
import run_summary
from llm_router import LlmQuotaWaiting

NOW = dt.datetime.now(dt.timezone.utc).isoformat()
WAITING = LlmQuotaWaiting(
    "WAITING_FOR_LLM_QUOTA: every approved Gemini writing model is blocked (per-minute/token quota)",
    kind="per_minute_model", scope="model", blocked_until="2026-10-18T07:00:00+00:00",
    models_attempted=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"], quota_model="gemini-3.1-flash-lite",
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)
    monkeypatch.setattr(cloud_pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(notify_failure, "ROOT", tmp_path)
    return tmp_path


def write_history(root: Path, records: list[dict]) -> Path:
    path = root / "config" / "topics_history.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def story_folder(root: Path, name: str) -> Path:
    folder = root / "stories" / name
    (folder / "logs").mkdir(parents=True)
    (folder / "logs" / "topic.json").write_text("{}", encoding="utf-8")
    return folder


OASIS = {"slug": "oasis-life-gulf-falaj-palm", "status": "GENERATING",
         "story_path": "stories/2026-10-17_oasis-life-gulf-falaj-palm", "created_at": NOW}
DHOFAR = {"slug": "dhofar-incense-network", "status": "pending_approval",
          "story_path": "stories/2026-10-07_dhofar-incense-network"}


def test_active_episode_wins_over_a_stale_dhofar_entry_in_either_order(repo):
    story_folder(repo, "2026-10-17_oasis-life-gulf-falaj-palm")
    for history in ([DHOFAR, OASIS], [OASIS, DHOFAR]):
        assert episode_selection.active_episode(history, repo)["slug"] == OASIS["slug"]


def test_zombie_entry_is_ignored_before_any_artifact_search(repo):
    assert episode_selection.active_episode([DHOFAR], repo) is None


def test_automation_state_active_episode_takes_priority(repo):
    story_folder(repo, "2026-10-17_a")
    story_folder(repo, "2026-10-17_b")
    history = [
        {"slug": "a", "status": "GENERATING", "story_path": "stories/2026-10-17_a", "created_at": NOW},
        {"slug": "b", "status": "GENERATING", "story_path": "stories/2026-10-17_b", "created_at": NOW},
    ]
    state = {"a": {"approval_status": "GENERATING", "story_path": "stories/2026-10-17_a"}}
    assert episode_selection.active_episode(history, repo, state)["slug"] == "a"
    assert episode_selection.active_episode(history, repo)["slug"] == "b"


@pytest.mark.parametrize("status", ["GENERATING", "FAILED_RETRYABLE", "WAITING_FOR_LLM_QUOTA", "drafting"])
def test_active_states_resume_the_same_episode_and_never_pick_a_new_topic(repo, status):
    story_folder(repo, "2026-10-17_oasis-life-gulf-falaj-palm")
    write_history(repo, [dict(OASIS, status=status)])
    chooser = mock.Mock(side_effect=AssertionError("a new topic must not be selected"))
    resume = mock.Mock(return_value=repo / OASIS["story_path"])
    with mock.patch.object(generate_daily, "choose_valid_topic", chooser), \
            mock.patch.object(generate_daily, "resume_partial_story", resume):
        generate_daily.create_daily_story()
    chooser.assert_not_called()
    assert resume.call_args[0][0] == (repo / OASIS["story_path"]).resolve()


def test_a_completed_episode_does_not_block_a_new_topic(repo):
    write_history(repo, [dict(OASIS, status="REVIEW_PENDING", archive_identifier="item")])
    assert episode_selection.active_episode(json.loads((repo / "config/topics_history.json").read_text()), repo) is None


def test_new_video_request_announces_resuming_instead_of_starting_a_second_video(repo):
    story_folder(repo, "2026-10-17_oasis-life-gulf-falaj-palm")
    write_history(repo, [OASIS])
    sent = []
    with mock.patch.object(cloud_pipeline, "send_message", side_effect=lambda text, *a: sent.append(text)):
        active = cloud_pipeline.announce_resume_of_active_episode()
    assert active["slug"] == OASIS["slug"]
    assert len(sent) == 1 and "يوجد فيديو غير مكتمل" in sent[0] and "من آخر نقطة محفوظة" in sent[0]


def test_no_resume_notice_when_nothing_is_unfinished(repo):
    write_history(repo, [dict(OASIS, status="PUBLISHED", archive_identifier="x")])
    with mock.patch.object(cloud_pipeline, "send_message") as send:
        assert cloud_pipeline.announce_resume_of_active_episode() is None
    send.assert_not_called()


# ---- planned quota pause --------------------------------------------------------------------------

def test_quota_exhaustion_parks_the_episode_as_waiting_and_exits_with_the_planned_code(repo, capsys):
    folder = story_folder(repo, "2026-10-17_oasis-life-gulf-falaj-palm")
    history_path = write_history(repo, [OASIS])

    def run():
        generate_daily._remember_story(folder)
        run_summary.stage("packaging")
        raise WAITING

    with mock.patch.object(generate_daily, "_main", side_effect=run), \
            mock.patch.object(generate_daily.state_store, "ensure_hydrated"):
        with pytest.raises(SystemExit) as exit_info:
            generate_daily.main()
    assert exit_info.value.code == generate_daily.EXIT_WAITING_FOR_QUOTA == 75
    record = json.loads(history_path.read_text(encoding="utf-8"))[0]
    assert record["status"] == "WAITING_FOR_LLM_QUOTA"
    assert record["waiting_stage"] == "packaging" and record["quota_kind"] == "per_minute_model"
    assert record["story_path"] == OASIS["story_path"]                      # same episode, same checkpoint
    summary = run_summary.load()
    assert summary["status"] == "waiting_for_quota" and summary["waiting_for_quota"] is True
    assert summary["resume_stage"] == "packaging" and summary["quota_kind"] == "per_minute_model"
    assert summary["quota_model"] == "gemini-3.1-flash-lite" and summary["retryable"] is True
    assert "WAITING_FOR_LLM_QUOTA" in capsys.readouterr().out


def test_a_waiting_episode_is_resumed_and_its_wait_fields_are_cleared(repo):
    folder = story_folder(repo, "2026-10-17_oasis-life-gulf-falaj-palm")
    history_path = write_history(repo, [dict(OASIS, status="WAITING_FOR_LLM_QUOTA", waiting_stage="packaging",
                                              blocked_until="x", quota_kind="daily", waiting_for="llm_quota")])
    generate_daily.upsert_history_checkpoint(history_path, folder, {"slug": OASIS["slug"], "topic_ar": "t", "angle_ar": "a"}, "GENERATING")
    record = json.loads(history_path.read_text(encoding="utf-8"))[0]
    assert record["status"] == "GENERATING"
    assert not {"waiting_stage", "blocked_until", "quota_kind", "waiting_for"} & set(record)


def test_exit_code_75_from_the_generator_is_reported_as_a_planned_wait(repo):
    with pytest.raises(RuntimeError, match="WAITING_FOR_LLM_QUOTA"):
        cloud_pipeline.run([sys.executable, "-c", "import sys; sys.exit(75)"], "t.log")
    with pytest.raises(RuntimeError, match="Command failed"):
        cloud_pipeline.run([sys.executable, "-c", "import sys; sys.exit(1)"], "t.log")


# ---- run summary ----------------------------------------------------------------------------------

def test_run_summary_records_success_failure_counters_and_stages(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "49")
    run_summary.start(workflow="Sleep History Production", action="produce")
    run_summary.stage("research")
    run_summary.count("gemini", 3)
    run_summary.reused("outline")
    run_summary.regenerated("packaging")
    run_summary.set_search_provider("exa")
    run_summary.update(restored_from_checkpoint=True, episode_id="ep_x")
    summary = run_summary.load()
    assert summary["run_id"] == "49" and summary["current_stage"] == "research"
    assert summary["external_calls_summary"]["gemini"] == 3
    assert summary["reused_stages"] == ["outline"] and summary["regenerated_stages"] == ["packaging"]
    assert summary["search_provider_used"] == "exa" and summary["restored_from_checkpoint"] is True
    run_summary.finish()
    assert run_summary.load()["status"] == "success"


def test_failure_keeps_the_specific_root_error_not_the_generic_wrapper(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "49")
    run_summary.start(workflow="generate_daily", action="generate")
    run_summary.stage("packaging")
    run_summary.fail(RuntimeError("YOUTUBE_PACKAGING_VALIDATION_FAILED: episode_title_core (has 12 Arabic words)"))
    run_summary.fail(RuntimeError("Command failed (1); diagnostic log: logs/cloud-generate.log"))   # parent process
    summary = run_summary.load()
    assert summary["final_root_error"].startswith("YOUTUBE_PACKAGING_VALIDATION_FAILED: episode_title_core")
    assert summary["failed_stage"] == "packaging" and summary["error_category"] == "VALIDATION"


def test_blocked_cost_and_owner_action_are_recorded(monkeypatch):
    run_summary.start(workflow="x", action="publish")
    run_summary.update(owner_action_required=True, owner_action="REAUTHORIZE_YOUTUBE_OAUTH")
    run_summary.fail(RuntimeError("BLOCKED_COST: unapproved LLM models: ['paid']"))
    summary = run_summary.load()
    assert summary["cost_blocked"] is True and summary["retryable"] is False
    assert summary["owner_action_required"] is True


def test_summary_never_contains_secrets(monkeypatch):
    run_summary.start(workflow="x", action="y")
    run_summary.fail(RuntimeError(
        "Gemini HTTP 400 https://generativelanguage.googleapis.com/v1beta/models/m:generateContent"
        "?key=AIzaSyA1234567890123456789012345 Authorization: Bearer abc.def token=sk-123456 "
        "bot 123456789:ABCdefGhIJKlmNoPQRsTUVwxyz0123456789"))
    text = (run_summary.SUMMARY_PATH).read_text(encoding="utf-8")
    for secret in ("AIzaSyA1234567890123456789012345", "abc.def", "sk-123456", "ABCdefGhIJKlmNoPQRsTUVwxyz0123456789"):
        assert secret not in text


def test_observability_failure_never_breaks_production(monkeypatch, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(run_summary, "SUMMARY_PATH", blocked / "run_summary.json")
    run_summary.start(workflow="x", action="y")
    run_summary.stage("z")
    run_summary.count("gemini")
    run_summary.fail(RuntimeError("boom"))
    run_summary.finish()


def test_cloud_pipeline_main_writes_the_final_error_to_the_summary(repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cloud_pipeline.py", "produce"])
    with mock.patch.object(cloud_pipeline, "dispatch_command", side_effect=RuntimeError("TTS_WAITING_FOR_QUOTA: Gemini")), \
            mock.patch.object(cloud_pipeline.state_store, "ensure_hydrated"):
        with pytest.raises(RuntimeError):
            cloud_pipeline.main()
    summary = run_summary.load()
    assert summary["status"] == "waiting_for_quota" and summary["action"] == "produce"


# ---- Telegram -------------------------------------------------------------------------------------

OLD_429_LOG = (
    "Gemini temporary HTTP 429; retrying in 15s (2/3): RESOURCE_EXHAUSTED GenerateRequestsPerMinutePerProjectPerModel-FreeTier\n"
    "RuntimeError: Gemini HTTP 429: RESOURCE_EXHAUSTED quota\n"
    "later work happened\n"
    "RuntimeError: YOUTUBE_PACKAGING_VALIDATION_FAILED: episode_title_core\n"
)


def test_telegram_shows_the_final_root_error_not_an_older_429_from_the_log(repo, monkeypatch):
    (repo / "logs").mkdir()
    (repo / "logs" / "cloud-generate.log").write_text(OLD_429_LOG, encoding="utf-8")
    monkeypatch.setenv("GITHUB_RUN_ID", "49")
    run_summary.start(workflow="generate_daily", action="generate")
    run_summary.update(story_path=OASIS["story_path"], checkpoint_saved=True)
    run_summary.stage("packaging")
    run_summary.fail(RuntimeError("YOUTUBE_PACKAGING_VALIDATION_FAILED: episode_title_core (has 12 Arabic words)"))
    message = notify_failure.build_message("https://github.com/x/actions/runs/49")
    assert "YOUTUBE_PACKAGING_VALIDATION_FAILED: episode_title_core" in message
    assert "429" not in message and "RESOURCE_EXHAUSTED" not in message
    assert "oasis-life-gulf-falaj-palm" in message and "حزمة YouTube" in message
    assert "Traceback" not in message and "https://github.com/x/actions/runs/49" in message


def test_planned_quota_pause_message_is_calm_and_says_work_is_saved(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "49")
    run_summary.start(workflow="generate_daily", action="generate")
    run_summary.stage("packaging")
    run_summary.fail(WAITING)
    message = notify_failure.build_message("https://github.com/x/actions/runs/49")
    assert message.startswith("<b>⏸️ توقفت الحلقة مؤقتًا بسبب حصة نماذج الكتابة.</b>")
    assert "الموضوع والبحث والعمل المنجز محفوظ" in message
    assert "التشغيل القادم سيكمل من نفس النقطة ولن يبدأ من الصفر" in message
    assert "لا يلزم أي إجراء منك" in message
    assert "⚠️" not in message and "توقفت أتمتة" not in message


def test_failure_message_states_retry_cost_and_owner_action(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "7")
    run_summary.start(workflow="x", action="produce")
    run_summary.update(checkpoint_saved=True)
    run_summary.fail(RuntimeError("BLOCKED_COST: unapproved LLM models: ['paid']"))
    message = notify_failure.build_message("u")
    assert "محظور بسياسة التكلفة" in message and "لن يُحل بإعادة التشغيل" in message


def test_old_runs_without_a_summary_fall_back_to_log_parsing(repo, monkeypatch):
    (repo / "logs").mkdir()
    (repo / "logs" / "cloud-generate.log").write_text("RuntimeError: Gemini HTTP 503: UNAVAILABLE\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    message = notify_failure.build_message("u")
    assert "HTTP 503" in message and "السبب المختصر" in message


def test_a_summary_from_another_run_is_not_used(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "48")
    run_summary.start(workflow="x", action="produce")
    run_summary.fail(RuntimeError("old failure"))
    monkeypatch.setenv("GITHUB_RUN_ID", "50")
    (repo / "logs").mkdir()
    (repo / "logs" / "a.log").write_text("RuntimeError: current failure\n", encoding="utf-8")
    message = notify_failure.build_message("u")
    assert "old failure" not in message and "current failure" in message
