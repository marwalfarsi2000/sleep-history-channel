from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import datetime as dt
from pathlib import Path

from archive_storage import (
    archive_metadata,
    delete_rejected,
    finalize_existing_upload,
    set_index_status,
    upload_story,
)


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
    if return_code:
        raise RuntimeError(
            f"Command failed ({return_code}); diagnostic log: {log_path.relative_to(ROOT)}"
        )
    return combined


def ensure_background_audio() -> None:
    target = ROOT / SETTINGS.get("background_audio_file", "assets/audio/pink-noise.m4a")
    if target.exists() and target.stat().st_size > 1024:
        return
    url = os.environ.get("PINK_NOISE_URL", "").strip()
    if not url:
        raise RuntimeError("PINK_NOISE_URL is missing and the background audio is not bundled")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as response, temporary.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)
    if temporary.stat().st_size <= 1024:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Downloaded pink-noise asset is empty")
    temporary.replace(target)


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
    return record


def produce() -> dict:
    from state_machine import State, normalize_state
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
    return review_record(story, upload_story(story))


def notify_saved_review() -> dict:
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
    current_remote = normalize_state(remote_status(identifier))
    allowed_states = {State.REVIEW_PENDING, State.APPROVED_FOR_PUBLISH, State.PUBLISHING, State.PUBLISHED}
    if current_remote not in allowed_states:
        raise RuntimeError(f"HUMAN_APPROVAL_GATE_FAILED: archive item status {current_remote!r} is not publishable")

    from youtube_publish import publish_archive_item
    if current_remote == State.REVIEW_PENDING:
        modify_remote(identifier, {
            "approval_status": State.APPROVED_FOR_PUBLISH,
            "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        set_index_status(identifier, State.APPROVED_FOR_PUBLISH)

    result = publish_archive_item(identifier)
    set_index_status(
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
    return review_record(story, upload_story(story))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=[
        "produce", "notify-review", "notify-existing-review", "resume-review",
        "publish", "reject", "regenerate",
    ])
    parser.add_argument("--identifier")
    args = parser.parse_args()
    if args.command == "produce":
        result = produce()
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
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

