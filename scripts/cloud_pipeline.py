from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import sys
import urllib.parse
import urllib.request
import datetime as dt
from pathlib import Path

from archive_storage import (
    advance_index_status,
    archive_metadata,
    delete_rejected,
    finalize_existing_upload,
    set_index_status,
    upload_story,
)


import episode_selection
import run_summary
import state_store


WAITING_EXIT_CODE = 75
ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(command: list[str], log_name: str) -> str:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / log_name
    output: list[str] = []
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout is None:
        raise RuntimeError("Unable to capture child process output")
    with log_path.open("w", encoding="utf-8") as log:
        for line in process.stdout:
            output.append(line)
            log.write(line)
            log.flush()
            print(line, end="", flush=True)
    process.stdout.close()
    return_code = process.wait()
    combined = "".join(output)
    if return_code == WAITING_EXIT_CODE:
        raise RuntimeError(
            f"WAITING_FOR_LLM_QUOTA: the episode is saved and will resume; diagnostic log: {log_path.relative_to(ROOT)}"
        )
    if return_code:
        raise RuntimeError(
            f"Command failed ({return_code}); diagnostic log: {log_path.relative_to(ROOT)}"
        )
    return combined


def ensure_background_audio() -> None:
    import background_audio
    background_audio.ensure_background_audio(ROOT, SETTINGS.get("background_audio_file"))


def telegram(method: str, payload: dict) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected {method}")
    return result


def send_message(text: str, keyboard: dict | None = None) -> None:
    run_summary.count("telegram")
    payload = {
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    telegram("sendMessage", payload)


def review_keyboard(identifier: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "🚀 اعتماد ونشر", "callback_data": f"publish:{identifier}"}],
        [
            {"text": "🔄 إعادة التوليد", "callback_data": f"regenerate:{identifier}"},
            {"text": "❌ رفض", "callback_data": f"reject:{identifier}"},
        ],
    ]}


def notify_review(record: dict) -> None:
    minutes = round(float(record.get("video_duration_seconds") or 0) / 60, 1)
    review_url = record.get("review_url") or record.get("item_url") or record.get("video_url")
    send_message(
        "<b>حلقة جديدة جاهزة للمراجعة</b>\n\n"
        f"{record['title']}\n"
        f"المدة: {minutes} دقيقة\n"
        f"<a href=\"{review_url}\">▶️ فتح نسخة المراجعة على Internet Archive</a>\n\n"
        "الحالة: REVIEW_PENDING. لن تُنشر على YouTube دون اعتمادك، "
        "وعند الرفض يُحذف عنصر الأرشيف. رابط الأرشيف قابل للوصول قبل الاعتماد.",
        review_keyboard(record["identifier"]),
    )


def notify_existing_review(identifier: str) -> dict:
    from state_machine import State, normalize_state
    payload = archive_metadata(identifier)
    metadata = payload.get("metadata") or {}
    status = normalize_state(str(metadata.get("approval_status") or ""))
    if status != State.REVIEW_PENDING:
        raise RuntimeError("HUMAN_APPROVAL_GATE_FAILED: archive item is not REVIEW_PENDING")
    record = {
        "identifier": identifier,
        "title": str(metadata.get("title") or identifier),
        "video_duration_seconds": float(metadata.get("video_duration_seconds") or 0),
        "review_url": f"https://archive.org/details/{identifier}",
        "status": State.REVIEW_PENDING,
    }
    notify_review(record)
    return record


def resume_review() -> dict:
    from state_machine import State, normalize_state
    history = json.loads((ROOT / "config" / "topics_history.json").read_text(encoding="utf-8"))
    candidate = next((item for item in reversed(history) if normalize_state(item.get("status", "")) in (State.REVIEW_PENDING, State.MEDIA_READY) and not item.get("archive_identifier") and item.get("story_path")), None)
    if not candidate:
        raise RuntimeError("No uploaded pending-review checkpoint is available")
    story = (ROOT / candidate["story_path"]).resolve()
    record = finalize_existing_upload(story)
    notify_review(record)
    return record


def persist_state(archive_identifier: str, **fields: object) -> bool:
    ok = state_store.save_episode_for_archive(archive_identifier, fields)
    if not ok:
        print(f"WARNING: episode state for {archive_identifier} was not persisted to automation-state", file=sys.stderr)
    return ok


def review_record(story: Path, archive_record: dict) -> dict:
    from state_machine import State
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    record = {
        "identifier": archive_record["identifier"],
        "story_path": story.relative_to(ROOT).as_posix(),
        "title": metadata["title"],
        "video_duration_seconds": metadata.get("video_duration_seconds", 0),
        "review_url": archive_record["item_url"],
        "status": State.REVIEW_PENDING,
    }
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    atomic_json(runtime / "review_record.json", record)
    persist_state(
        record["identifier"],
        approval_status=State.REVIEW_PENDING,
        story_path=record["story_path"],
        title=record["title"],
        review_url=record["review_url"],
    )
    return record


def announce_resume_of_active_episode() -> dict | None:
    """A new-video request never starts a second topic while one is unfinished."""
    try:
        history = json.loads((ROOT / "config" / "topics_history.json").read_text(encoding="utf-8"))
        state_episodes = state_store._load_state().get("episodes", {})
    except Exception:
        return None
    active = episode_selection.active_episode(history, ROOT, state_episodes)
    if not active:
        return None
    run_summary.update(story_path=str(active.get("story_path") or ""))
    try:
        send_message(
            "يوجد فيديو غير مكتمل؛ سأكمله أولًا من آخر نقطة محفوظة.\n"
            f"<code>{active.get('slug', '')}</code>"
        )
    except Exception as exc:
        print(f"Warning: resume notice not sent: {exc}", file=sys.stderr)
    return active


def produce() -> dict:
    from state_machine import State, normalize_state
    run_summary.stage("produce")
    announce_resume_of_active_episode()
    ensure_background_audio()
    output = run([sys.executable, str(ROOT / "scripts" / "generate_daily.py")], "cloud-generate.log")
    generated = None
    for line in reversed(output.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("story_path"):
            generated = candidate
            break
    if not generated:
        raise RuntimeError("Daily generator did not return a story path")
    story = Path(generated["story_path"]).resolve()
    run([sys.executable, str(ROOT / "scripts" / "pipeline.py"), "all", str(story)], "cloud-pipeline.log")
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    if normalize_state(metadata.get("approval_status", "")) != State.MEDIA_READY:
        raise RuntimeError(f"Production did not reach {State.MEDIA_READY}")
    # The verified video goes to Internet Archive as the durable review copy;
    # GitHub keeps only small state files and logs.
    run_summary.stage("archive")
    return review_record(story, upload_story(story))


SKIP_MARKER = ROOT / ".runtime" / "produce_skipped.json"
MAX_AUTORECOVERY_ATTEMPTS = 1           # only one re-run, and only for transient external failures
AUTORECOVERY_BACKOFF_SECONDS = (30,)
AUTORECOVERY_DEADLINE_SECONDS = 4 * 3600  # never retry late in the 355-minute job
RETRY_EXIT_CODES_NEVER = ("WAITING_FOR_LLM_QUOTA", "TTS_WAITING_FOR_QUOTA")


def recovery_decision(summary: dict, attempts: int, elapsed: float) -> tuple[bool, int]:
    """Should this job re-run ``produce`` itself? Returns ``(retry, wait_seconds)``.

    Global recovery is intentionally tiny: only a transient external failure may re-run once.
    Validation/content failures must be repaired inside their own stage or stop with a checkpoint.
    Quota waits, cost blocks, owner actions and code defects are never retried here.
    """
    if summary.get("status") != "failed" or summary.get("recovery_policy") != "AUTO_RECOVERABLE":
        return False, 0
    if not summary.get("immediate_retry") or summary.get("waiting_for_quota") or summary.get("cost_blocked"):
        return False, 0
    if summary.get("owner_action_required"):
        return False, 0
    limit = min(MAX_AUTORECOVERY_ATTEMPTS, int(summary.get("max_attempts") or MAX_AUTORECOVERY_ATTEMPTS))
    if attempts >= limit or elapsed > AUTORECOVERY_DEADLINE_SECONDS:
        return False, 0
    stage = summary.get("failed_stage") or summary.get("current_stage")
    if not summary.get("checkpoint_saved") and stage not in ("topic_selection", "produce", None):
        return False, 0
    repeats = [entry for entry in summary.get("auto_recovery_log", []) if entry.get("error_code") == summary.get("error_code")]
    if len(repeats) >= limit:
        return False, 0
    return True, AUTORECOVERY_BACKOFF_SECONDS[min(attempts, len(AUTORECOVERY_BACKOFF_SECONDS) - 1)]


def produce_with_recovery() -> dict:
    """Run production with one checkpoint-reusing retry for transient external failures only."""
    started = time.monotonic()
    attempts = 0
    while True:
        try:
            return produce()
        except Exception:
            summary = run_summary.load()
            retry, wait = recovery_decision(summary, attempts, time.monotonic() - started)
            if not retry:
                raise
            attempts += 1
            print(
                f"AUTO_RECOVERY attempt {attempts}: {summary.get('error_code')} at {summary.get('failed_stage')}; "
                f"resuming from the saved checkpoint in {wait}s", file=sys.stderr, flush=True,
            )
            run_summary.begin_recovery(attempts, summary.get("error_code"), summary.get("failed_stage"), wait)
            time.sleep(wait)


def resume_only() -> dict:
    """Extra scheduled runs only finish an unfinished episode; they never start a new topic."""
    try:
        history = json.loads((ROOT / "config" / "topics_history.json").read_text(encoding="utf-8"))
        state_episodes = state_store._load_state().get("episodes", {})
    except Exception:
        history, state_episodes = [], {}
    active = episode_selection.active_episode(history, ROOT, state_episodes)
    reason = ""
    if not active:
        reason = "no unfinished episode"
    else:
        blocked_until = str(active.get("blocked_until") or "")
        try:
            if blocked_until and dt.datetime.fromisoformat(blocked_until) > dt.datetime.now(dt.timezone.utc):
                reason = f"episode is waiting for quota until {blocked_until}"
        except ValueError:
            pass
    if reason:
        SKIP_MARKER.parent.mkdir(parents=True, exist_ok=True)
        SKIP_MARKER.write_text(json.dumps({"skipped": True, "reason": reason}), encoding="utf-8")
        print(f"RESUME_ONLY skipped: {reason}", flush=True)
        return {"ok": True, "skipped": reason}
    return produce_with_recovery()


def notify_saved_review() -> dict:
    if SKIP_MARKER.exists():
        return {"ok": True, "skipped": "resume-only run had nothing to resume"}
    record_path = ROOT / ".runtime" / "review_record.json"
    if not record_path.exists():
        raise RuntimeError("Review record is missing")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    notify_review(record)
    return record


def remote_status(identifier: str) -> str:
    return str((archive_metadata(identifier).get("metadata") or {}).get("approval_status", ""))


def modify_remote(identifier: str, fields: dict) -> None:
    from archive_storage import get_archive_item
    from error_classifier import retry_transient
    def _update():
        item = get_archive_item(identifier, access_key=os.environ["IAS3_ACCESS_KEY"], secret_key=os.environ["IAS3_SECRET_KEY"])
        response = item.modify_metadata(fields)
        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError(f"Internet Archive metadata update failed with HTTP {response.status_code}")
        return response
    retry_transient(_update)


def download_file(identifier: str, filename: str, destination: Path) -> None:
    url = "https://archive.org/download/{}/{}".format(
        urllib.parse.quote(identifier, safe=""), urllib.parse.quote(filename)
    )
    with urllib.request.urlopen(url, timeout=600) as response, destination.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)


def publish(identifier: str) -> dict:
    from state_machine import State, normalize_state
    from archive_recovery import ensure_index_record

    run_summary.stage("publish")
    current_remote = normalize_state(remote_status(identifier))
    allowed_states = {State.REVIEW_PENDING, State.APPROVED_FOR_PUBLISH, State.PUBLISHING, State.PUBLISHED}
    if current_remote not in allowed_states:
        raise RuntimeError(f"HUMAN_APPROVAL_GATE_FAILED: archive item status {current_remote!r} is not publishable")

    ensure_index_record(
        identifier,
        access_key=os.environ.get("IAS3_ACCESS_KEY", ""),
        secret_key=os.environ.get("IAS3_SECRET_KEY", "")
    )

    payload = archive_metadata(identifier)
    metadata = payload.get("metadata") or {}

    if normalize_state(str(metadata.get("approval_status") or "")) != current_remote:
        raise RuntimeError("CONSISTENCY_CHECK_FAILED: local and remote approval status mismatch")

    if current_remote == State.REVIEW_PENDING:
        if not metadata.get("title"):
            raise RuntimeError("PREFLIGHT_VALIDATION_FAILED: archive metadata missing title")
        if metadata.get("youtube_packaging_status"):
            if metadata.get("youtube_packaging_validation_status") != "passed":
                raise RuntimeError("PREFLIGHT_VALIDATION_FAILED: YouTube packaging validation did not pass")
            if metadata.get("thumbnail_text_render_status") != "passed":
                raise RuntimeError("PREFLIGHT_VALIDATION_FAILED: thumbnail text render status is not passed")

    from youtube_publish import publish_archive_item
    result = publish_archive_item(identifier)

    if result.get("requires_owner_action"):
        run_summary.update(owner_action_required=True, owner_action=result.get("action"))
        persist_state(
            identifier,
            approval_status=current_remote,
            publish_blocked=result.get("status", "OWNER_ACTION_REQUIRED"),
            publish_blocked_action=result.get("action"),
            youtube_video_id=result.get("video_id"),
        )
        return result

    advance_index_status(
        identifier,
        State.PUBLISHED,
        youtube_video_id=result["video_id"],
        youtube_url=result["url"],
        published_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=4))).isoformat(),
    )
    modify_remote(identifier, {
        "approval_status": State.PUBLISHED,
        "output_verification_status": "passed",
        "youtube_video_id": result["video_id"],
        "youtube_url": result["url"],
    })
    run_summary.update(youtube_video_id=result["video_id"], archive_identifier=identifier)
    persist_state(
        identifier,
        approval_status=State.PUBLISHED,
        publish_blocked=None,
        youtube_video_id=result["video_id"],
        youtube_url=result["url"],
    )
    send_message(
        "<b>تم اعتماد الحلقة ونشرها ✅</b>\n"
        f"<a href=\"{result['url']}\">▶️ مشاهدة الفيديو على YouTube</a>\n"
        f"<a href=\"https://archive.org/details/{identifier}\">📚 النسخة الكاملة المؤرشفة</a>"
    )
    return result


def reject(identifier: str) -> dict:
    from state_machine import State, normalize_state
    if normalize_state(remote_status(identifier)) != State.REVIEW_PENDING:
        raise RuntimeError("Only REVIEW_PENDING archive items can be rejected and deleted")
    delete_rejected(identifier)
    set_index_status(identifier, State.REJECTED)
    persist_state(identifier, approval_status=State.REJECTED)
    send_message(f"تم رفض الحلقة وحذف عنصرها من Internet Archive. لم تُنشر على YouTube.\n<code>{identifier}</code>")
    return {"identifier": identifier, "status": State.REJECTED}


REVIEW_SOURCE_FILES = (
    "01_title.txt", "02_description.txt", "03_sources.md", "04_research_notes.md",
    "05_story_clean.md", "06_story_tts_annotated.md", "07_story_tts_api.md",
    "08_image_prompt_en.txt", "15_metadata.json", "16_fact_check.md",
)


def regenerate(identifier: str) -> dict:
    from state_machine import State, normalize_state
    ensure_background_audio()
    if normalize_state(remote_status(identifier)) != State.REVIEW_PENDING:
        raise RuntimeError("Only REVIEW_PENDING archive items can be regenerated")
    slug = str((archive_metadata(identifier).get("metadata") or {}).get("slug") or identifier)
    story = ROOT / "stories" / slug
    story.mkdir(parents=True, exist_ok=True)
    for name in REVIEW_SOURCE_FILES:
        download_file(identifier, name, story / name)
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    if metadata.get("youtube_packaging_status"):
        download_file(identifier, "08_thumbnail_prompt_en.txt", story / "08_thumbnail_prompt_en.txt")
    metadata["approval_status"] = State.GENERATING
    atomic_json(story / "15_metadata.json", metadata)
    (story / "09_image_source.md").write_text("# مصدر الصورة\n\nستُعاد توليد الصورة آليًا.\n", encoding="utf-8")
    (story / "17_status.md").write_text(f"# الحالة\n\n- الحالة الحالية: `{State.GENERATING}`\n", encoding="utf-8")
    run([sys.executable, str(ROOT / "scripts" / "pipeline.py"), "all", str(story)], "cloud-regenerate.log")
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    if normalize_state(metadata.get("approval_status", "")) != State.MEDIA_READY:
        raise RuntimeError(f"Regeneration did not reach {State.MEDIA_READY}")
    run_summary.stage("archive")
    return review_record(story, upload_story(story))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=[
        "produce", "notify-review", "notify-existing-review", "resume-review",
        "publish", "reject", "regenerate",
    ])
    parser.add_argument("--identifier")
    parser.add_argument("--resume-only", action="store_true", help="produce: only finish an unfinished episode")
    args = parser.parse_args()
    state_store.ensure_hydrated()
    if args.command == "produce":
        # Hydration restores the branch copy of the history; retire zombie records again so they persist.
        import restore_failure_checkpoint
        restore_failure_checkpoint.migrate_history(ROOT)
    run_summary.start(workflow="cloud_pipeline", action=args.command, archive_identifier=args.identifier or "")
    try:
        result = dispatch_command(args, parser)
    except BaseException as exc:
        run_summary.fail(exc)
        raise
    run_summary.finish()
    print(json.dumps(result, ensure_ascii=False))


def dispatch_command(args, parser) -> dict:
    if args.command == "produce":
        result = resume_only() if args.resume_only else produce_with_recovery()
    elif args.command == "notify-review":
        result = notify_saved_review()
    elif args.command == "resume-review":
        result = resume_review()
    elif args.command == "notify-existing-review":
        if not args.identifier:
            parser.error("--identifier is required")
        result = notify_existing_review(args.identifier)
    elif args.command == "publish":
        if not args.identifier:
            parser.error("--identifier is required")
        result = publish(args.identifier)
    elif args.command == "reject":
        if not args.identifier:
            parser.error("--identifier is required")
        result = reject(args.identifier)
    else:
        if not args.identifier:
            parser.error("--identifier is required")
        result = regenerate(args.identifier)
    return result


if __name__ == "__main__":
    main()

