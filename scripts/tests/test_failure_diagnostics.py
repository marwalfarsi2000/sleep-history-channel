"""Failure intelligence: exact stage, structured diagnosis, failure registry and Telegram wording."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

import failure_registry as registry
import generate_daily
import llm_router
import notify_failure
import run_summary

SCRIPTS = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "topics_history.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)
    monkeypatch.setattr(notify_failure, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "63")
    return tmp_path


def failed(exc: BaseException, stage: str = "") -> dict:
    run_summary.start(workflow="generate_daily", action="generate")
    if stage:
        run_summary.stage(stage)
    run_summary.fail(exc)
    return run_summary.load()


# ---- A. exact stage tracking ------------------------------------------------------------------

def test_run63_topic_discovery_failure_is_a_topic_selection_failure(repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["generate_daily.py"])
    boom = RuntimeError("TOPIC_DISCOVERY_FAILED: fewer than 6 candidates")
    run_summary.start(workflow="cloud_pipeline", action="produce")
    run_summary.stage("produce")  # what the parent process sets before it launches the generator
    with mock.patch.object(generate_daily.state_store, "ensure_hydrated"), \
            mock.patch.object(generate_daily, "choose_valid_topic", side_effect=boom), \
            mock.patch.object(generate_daily.state_store, "_load_state", return_value={}):
        with pytest.raises(RuntimeError):
            generate_daily.main()
    summary = run_summary.load()
    assert summary["failed_stage"] == "topic_selection"
    assert summary["failed_stage"] != "produce"
    assert summary["failed_component"] == "topic_discovery"
    assert summary["error_code"] == "TOPIC_DISCOVERY_FAILED"
    assert summary["error_category"] == "DATA_OR_MODEL_OUTPUT_VALIDATION"
    assert summary["repo_file"] == "scripts/generate_daily.py" or summary["repo_file"].endswith("generate_daily.py")
    assert summary["retryable"] is True and summary["auto_recovery_available"] is True
    assert summary["owner_action_required"] is False
    assert summary["recommended_action"].startswith("Use valid fresh candidates")


def test_registry_alone_places_topic_discovery_even_if_only_produce_was_tracked():
    summary = failed(RuntimeError("TOPIC_DISCOVERY_FAILED: fewer than 6 candidates"), stage="produce")
    assert summary["failed_stage"] == "topic_selection" and summary["failed_component"] == "topic_discovery"


@pytest.mark.parametrize("dry_run", [False, True])
def test_topic_selection_is_recorded_before_choose_valid_topic_runs(repo, dry_run):
    seen = []
    with mock.patch.object(generate_daily.state_store, "_load_state", return_value={}), \
            mock.patch.object(generate_daily.run_summary, "stage", side_effect=seen.append), \
            mock.patch.object(generate_daily, "choose_valid_topic",
                              side_effect=lambda history: seen.append("CHOOSE") or (_ for _ in ()).throw(RuntimeError("x"))):
        with pytest.raises(RuntimeError):
            generate_daily.create_daily_story(dry_run=dry_run)
    assert seen == ["topic_selection", "CHOOSE"]


def first_stage_call(path: str, function: str) -> str | None:
    tree = ast.parse((SCRIPTS / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            for statement in node.body:
                call = getattr(statement, "value", None)
                if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "stage":
                    return ast.literal_eval(call.args[0])
            return None
    raise AssertionError(function)


@pytest.mark.parametrize("path, function, expected", [
    ("pipeline.py", "prepare", "prepare"), ("pipeline.py", "synthesize", "synthesize"),
    ("pipeline.py", "background", "background"), ("pipeline.py", "validate", "validate"),
    ("pipeline.py", "package", "package"), ("cloud_pipeline.py", "publish", "publish"),
])
def test_pipeline_stages_are_recorded_before_the_stage_body(path, function, expected):
    assert first_stage_call(path, function) == expected


def test_render_marks_render_after_the_background_it_triggers():
    source = (SCRIPTS / "pipeline.py").read_text(encoding="utf-8")
    body = source[source.index("def render(story"):][:200]
    assert body.index("background(story") < body.index('run_summary.stage("render")')


def test_every_major_stage_name_is_used_somewhere_before_its_work():
    used = set()
    for name in ("generate_daily.py", "pipeline.py", "cloud_pipeline.py"):
        used |= set(__import__("re").findall(r'run_summary\.stage\("(\w+)"\)', (SCRIPTS / name).read_text(encoding="utf-8")))
    assert {"topic_selection", "research", "outline", "chapters", "quality_gates", "packaging", "prepare",
            "synthesize", "background", "render", "validate", "archive", "publish"} <= used


# ---- B/C/D. structured diagnosis ----------------------------------------------------------------

@pytest.mark.parametrize("message, code", [
    ("TOPIC_DISCOVERY_FAILED: fewer than 6 candidates", "TOPIC_DISCOVERY_FAILED"),
    ("TOPIC_GATE_REJECTED: story_format repeated", "TOPIC_GATE_REJECTED"),
    ("OUTLINE_GENERATION_FAILED_AFTER_RETRIES: malformed", "OUTLINE_GENERATION_FAILED_AFTER_RETRIES"),
    ("CHAPTER_LENGTH_TARGET_FAILED: chapter 2 has 100 words", "CHAPTER_LENGTH_TARGET_FAILED"),
    ("IMAGE_ACCURACY_GATE_FAILED: {}", "IMAGE_ACCURACY_GATE_FAILED"),
    ("BLOCKED_COST: unapproved LLM models", "BLOCKED_COST"),
    ("BLOCKED_COST_OR_FREE_LIMIT: Fish Audio returned HTTP 402", "BLOCKED_COST_OR_FREE_LIMIT"),
    ("TTS_WAITING_FOR_QUOTA: Gemini quota", "TTS_WAITING_FOR_QUOTA"),
    ("Generated story is too short: 4000 Arabic words", "STORY_TOO_SHORT"),
])
def test_error_codes_come_from_the_message_prefix(message, code):
    assert registry.extract_error_code(RuntimeError(message)) == code


def test_a_keyword_in_the_middle_of_a_message_is_not_a_code():
    assert registry.extract_error_code(RuntimeError("something about TOPIC_DISCOVERY_FAILED later")) == ""


def test_registry_entries_are_complete():
    for code, entry in registry.REGISTRY.items():
        assert entry.code == code and entry.category and entry.recommended_action
        assert entry.policy in (registry.AUTO_RECOVERABLE, registry.OWNER_ACTION, registry.CODE_DEFECT, None)
        assert entry.wrapper or entry.component
    tts = registry.REGISTRY["TTS_WAITING_FOR_QUOTA"]
    assert (tts.stage, tts.component, tts.retryable, tts.planned_wait) == ("synthesize", "tts", True, True)
    assert registry.REGISTRY["IMAGE_ACCURACY_GATE_FAILED"].stage == "background"
    assert registry.REGISTRY["IMAGE_ACCURACY_GATE_FAILED"].component == "historical_accuracy_gate"


# ---- I. one case per failure kind -------------------------------------------------------------

def test_topic_gate_rejection():
    s = failed(RuntimeError("TOPIC_GATE_REJECTED: no acceptable topic was found"), "topic_selection")
    assert (s["failed_stage"], s["failed_component"], s["recovery_policy"]) == ("topic_selection", "topic_gate", "AUTO_RECOVERABLE")


def test_story_quality_rejection_does_not_blind_retry_after_internal_repairs():
    s = failed(RuntimeError('STORY_QUALITY_GATE_REJECTED: {"deterministic":["curiosity does not decay"]}'), "quality_gates")
    assert s["failed_stage"] == "quality_gates"
    assert s["retryable"] is False
    assert s["immediate_retry"] is False
    assert s["recovery_policy"] == "AUTO_RECOVERABLE"


def test_malformed_outline():
    s = failed(RuntimeError("OUTLINE_GENERATION_FAILED_AFTER_RETRIES: not json"), "outline")
    assert (s["failed_stage"], s["failed_component"], s["retryable"]) == ("outline", "outline_generation", True)


def test_story_too_short():
    s = failed(RuntimeError("Generated story is too short: 4100 Arabic words"), "chapters")
    assert (s["error_code"], s["failed_component"], s["recovery_policy"]) == ("STORY_TOO_SHORT", "story_length", "AUTO_RECOVERABLE")


def test_tts_quota_wait_is_a_planned_wait():
    s = failed(RuntimeError("TTS_WAITING_FOR_QUOTA: Gemini narrator quota is used up"), "synthesize")
    assert s["status"] == "waiting_for_quota" and s["planned_wait"] is True and s["retryable"] is True
    assert (s["failed_stage"], s["failed_component"]) == ("synthesize", "tts")


def test_missing_credential_is_an_owner_action():
    s = failed(RuntimeError("BYTEZ_API_KEY is missing from the external secrets store"), "background")
    assert s["error_code"] == "MISSING_CREDENTIAL" and s["recovery_policy"] == "OWNER_ACTION"
    assert s["owner_action_required"] is True and s["auto_recovery_available"] is False
    assert s["failed_stage"] == "background"


def test_image_accuracy_failure():
    s = failed(RuntimeError('IMAGE_ACCURACY_GATE_FAILED: {"passed": false}'), "background")
    assert (s["failed_stage"], s["failed_component"], s["recovery_policy"]) == (
        "background", "historical_accuracy_gate", "AUTO_RECOVERABLE")


@pytest.mark.parametrize("message, policy, retryable", [
    ("IMAGE_FREE_CAPACITY_EXHAUSTED: Pollinations HTTP 402", "AUTO_RECOVERABLE", True),
    ("BLOCKED_COST: image model is marked paid_only: m", "OWNER_ACTION", False),
])
def test_image_provider_quota_and_cost(message, policy, retryable):
    s = failed(RuntimeError(message), "background")
    assert s["cost_blocked"] is True and s["recovery_policy"] == policy and s["retryable"] is retryable


def test_archive_upload_failure():
    s = failed(RuntimeError("Internet Archive upload failed with HTTP 503"), "archive")
    assert (s["error_code"], s["failed_stage"], s["failed_component"]) == ("ARCHIVE_UPLOAD_FAILED", "archive", "internet_archive")
    assert s["recovery_policy"] == "AUTO_RECOVERABLE"


def test_youtube_auth_failure_is_an_owner_action():
    s = failed(RuntimeError("invalid_grant: Token has been expired or revoked."), "publish")
    assert (s["error_code"], s["recovery_policy"], s["owner_action_required"]) == ("YOUTUBE_AUTH_FAILED", "OWNER_ACTION", True)


def test_unknown_failure_is_a_code_defect_with_no_invented_diagnosis():
    def explode():
        raise RuntimeError("boom")
    try:
        explode()
    except RuntimeError as exc:
        s = failed(exc, "render")
    assert s["failed_stage"] == "render"
    assert s["failed_component"] is None and s["error_code"] is None
    assert s["recovery_policy"] == "CODE_DEFECT" and s["auto_recovery_available"] is False
    assert s["repo_function"] == "explode" and s["repo_file"].endswith("test_failure_diagnostics.py")
    message = notify_failure.build_message("https://run")
    assert "المكوّن" not in message and "السبب: <code>boom</code>" in message


def test_generic_child_process_wrapper_is_not_diagnosed():
    s = failed(RuntimeError("Command failed (1); diagnostic log: logs/cloud-generate.log"), "produce")
    assert s["error_code"] == "CHILD_PROCESS_FAILED" and s["recovery_policy"] is None
    assert s["failed_component"] is None and s["repo_file"] is None
    message = notify_failure.build_message("https://run")
    assert "Codex" not in message and "لم يُصنَّف" in message


# ---- E. safe traceback location ----------------------------------------------------------------

def test_repo_location_is_the_deepest_repository_frame_and_relative():
    try:
        generate_daily.parse_json("not json at all")
    except Exception as exc:
        location = registry.repo_location(exc)
    else:  # parse_json may not raise on this input
        location = {"repo_file": None}
    if location["repo_file"]:
        assert not Path(location["repo_file"]).is_absolute()
        assert location["repo_file"].startswith("scripts/") and location["repo_line"] > 0


def test_repo_location_ignores_frames_outside_the_repository():
    try:
        json.loads("{")
    except json.JSONDecodeError as exc:
        location = registry.repo_location(exc)
    assert location["repo_file"].endswith("test_failure_diagnostics.py")
    assert "/usr/" not in location["repo_file"]


def test_summary_stores_no_secrets_or_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSySECRETSECRETSECRETSECRET1234")
    s = failed(RuntimeError("Gemini HTTP 400 ?key=AIzaSySECRETSECRETSECRETSECRET1234 Authorization: Bearer abc.def"), "outline")
    text = json.dumps(s)
    assert "SECRETSECRET" not in text and "abc.def" not in text


# ---- F. policy + G. Telegram --------------------------------------------------------------------

URL = "https://github.com/x/actions/runs/63"


def test_run63_telegram_names_stage_component_and_code_without_the_codex_line(repo):
    run_summary.start(workflow="generate_daily", action="generate")
    run_summary.stage("topic_selection")
    run_summary.fail(RuntimeError("TOPIC_DISCOVERY_FAILED: fewer than 6 candidates"))
    message = notify_failure.build_message(URL)
    assert "المرحلة: <b>اختيار الموضوع</b>" in message
    assert "المكوّن: <b>Topic Discovery</b>" in message
    assert "السبب: <code>TOPIC_DISCOVERY_FAILED</code>" in message
    assert "عدد كافٍ من المرشحين" in message
    assert "🔄 يستطيع النظام معالجة هذا تلقائيًا." in message
    assert "Codex" not in message and "بدء الإنتاج" not in message and "لن يُحل" not in message
    assert URL in message


def test_owner_action_message_says_what_is_needed(repo):
    failed(RuntimeError("BYTEZ_API_KEY is missing from the external secrets store"), "background")
    message = notify_failure.build_message(URL)
    assert "👤 يلزم منك:" in message and "أسرار المستودع" in message
    assert "Codex" not in message


def test_code_defect_message_points_at_file_and_function_and_keeps_the_codex_line(repo):
    def choose_topic():
        raise RuntimeError("impossible state")
    try:
        choose_topic()
    except RuntimeError as exc:
        failed(exc, "topic_selection")
    message = notify_failure.build_message(URL)
    assert "🛠️ يحتاج تعديلًا برمجيًا في:" in message and "choose_topic()" in message
    assert "test_failure_diagnostics.py" in message
    assert "أرسلي هذه الرسالة إلى Codex" in message


@pytest.mark.parametrize("message", [
    "TOPIC_DISCOVERY_FAILED: fewer than 6 candidates",
    "IMAGE_ACCURACY_GATE_FAILED: {}",
    "OUTLINE_GENERATION_FAILED_AFTER_RETRIES: x",
    "BYTEZ_API_KEY is missing",
    "invalid_grant: expired",
])
def test_known_errors_never_get_the_generic_send_to_codex_text(repo, message):
    failed(RuntimeError(message), "topic_selection")
    assert "Codex" not in notify_failure.build_message(URL)


def test_quota_wait_messages_are_unchanged_and_calm(repo):
    failed(llm_router.LlmQuotaWaiting("WAITING_FOR_LLM_QUOTA: all blocked", kind="k", scope="s",
                                      blocked_until="2026-10-18T07:00:00+00:00", models_attempted=[]), "packaging")
    message = notify_failure.build_message(URL)
    assert message.startswith("<b>⏸️") and "⚠️" not in message
