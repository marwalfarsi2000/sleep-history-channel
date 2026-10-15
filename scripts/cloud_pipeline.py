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


def notify_review(record: dict) -> None:
    minutes = round(float(record.get("video_duration_seconds") or 0) / 60, 1)
    review_url = record.get("review_url") or record.get("video_url") or record.get("item_url")
    storage_note = (
        "لن تُرفع إلى Internet Archive أو YouTube دون اعتمادك."
        if record.get("review_url")
        else "هذه حلقة قديمة مؤرشفة؛ لن تُنشر على YouTube دون اعتمادك."
    )
    keyboard = {"inline_keyboard": [
        [{"text": "🚀 اعتماد ونشر", "callback_data": f"publish:{record['identifier']}"}],
        [
            {"text": "🔄 إعادة التوليد", "callback_data": f"regenerate:{record['identifier']}"},
            {"text": "❌ رفض", "callback_data": f"reject:{record['identifier']}"},
        ],
    ]}
    send_message(
        "<b>حلقة جديدة جاهزة للمراجعة</b>\n\n"
        f"{record['title']}\n"
        f"المدة: {minutes} دقيقة\n"
        f"<a href=\"{review_url}\">▶️ فتح نسخة المراجعة</a>\n\n"
        + storage_note,
        keyboard,
    )


def resume_review() -> dict:
    history = json.loads((ROOT / "config" / "topics_history.json").read_text(encoding="utf-8"))
    candidate = next((item for item in reversed(history) if item.get("status") == "pending_approval" and not item.get("archive_identifier") and item.get("story_path")), None)
    if not candidate:
        raise RuntimeError("No uploaded pending-review checkpoint is available")
    story = (ROOT / candidate["story_path"]).resolve()
    record = finalize_existing_upload(story)
    notify_review(record)
    return record


def produce() -> dict:
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
    if metadata.get("approval_status") != "READY_FOR_APPROVAL":
        raise RuntimeError("Production did not reach READY_FOR_APPROVAL")
    review_id = os.environ.get("GITHUB_RUN_ID", "local")
    record = {
        "identifier": review_id,
        "story_path": story.relative_to(ROOT).as_posix(),
        "title": metadata["title"],
        "video_duration_seconds": metadata.get("video_duration_seconds", 0),
        "review_url": os.environ.get("RUN_URL", ""),
        "status": "READY_FOR_APPROVAL",
    }
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    atomic_json(runtime / "review_record.json", record)
    history_path = ROOT / "config" / "topics_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    for item in history:
        if item.get("slug") == metadata.get("slug"):
            item["status"] = "READY_FOR_APPROVAL"
            item["review_run_id"] = review_id
    atomic_json(history_path, history)
    return record


def notify_saved_review() -> dict:
    record_path = ROOT / ".runtime" / "review_record.json"
    if not record_path.exists():
        raise RuntimeError("Review record is missing")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    notify_review(record)
    return record


def restored_review_story() -> Path:
    candidates = []
    for metadata_path in ROOT.glob("stories/*/15_metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("approval_status") == "READY_FOR_APPROVAL":
            candidates.append((metadata_path.stat().st_mtime, metadata_path.parent))
    if not candidates:
        raise RuntimeError("No restored READY_FOR_APPROVAL story was found")
    return max(candidates)[1]


def download_file(identifier: str, filename: str, destination: Path) -> None:
    url = "https://archive.org/download/{}/{}".format(
        urllib.parse.quote(identifier, safe=""), urllib.parse.quote(filename)
    )
    with urllib.request.urlopen(url, timeout=600) as response, destination.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)


def publish(identifier: str) -> dict:
    from youtube_publish import publish_archive_item
    story = restored_review_story()
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    if metadata.get("approval_status") != "READY_FOR_APPROVAL":
        raise RuntimeError("HUMAN_APPROVAL_GATE_FAILED: story is not ready for approval")
    metadata["approval_status"] = "APPROVED_FOR_PUBLISH"
    metadata["approved_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_json(story / "15_metadata.json", metadata)
    archive_record = upload_story(story)
    archive_identifier = archive_record["identifier"]
    result = publish_archive_item(archive_identifier)
    set_index_status(archive_identifier, "COMPLETED", youtube_video_id=result["video_id"], youtube_url=result["url"])
    access = os.environ["IAS3_ACCESS_KEY"]
    secret = os.environ["IAS3_SECRET_KEY"]
    from internetarchive import get_item
    get_item(archive_identifier, access_key=access, secret_key=secret).modify_metadata({
        "approval_status": "COMPLETED",
        "output_verification_status": "passed",
        "youtube_video_id": result["video_id"],
        "youtube_url": result["url"],
    })
    send_message(
        "<b>تم اعتماد الحلقة ونشرها ✅</b>\n"
        f"<a href=\"{result['url']}\">▶️ مشاهدة الفيديو على YouTube</a>\n"
        f"<a href=\"https://archive.org/details/{archive_identifier}\">📚 النسخة الكاملة المؤرشفة</a>"
    )
    return result


def reject(identifier: str) -> dict:
    story = restored_review_story()
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    history_path = ROOT / "config" / "topics_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    for item in history:
        if item.get("slug") == metadata.get("slug"):
            item["status"] = "rejected"
    atomic_json(history_path, history)
    send_message(f"تم رفض الحلقة. لم تُرفع إلى Internet Archive أو YouTube.\n<code>{identifier}</code>")
    return {"identifier": identifier, "status": "rejected"}


def regenerate(identifier: str) -> dict:
    ensure_background_audio()
    story = restored_review_story()
    (story / "09_image_source.md").write_text("# مصدر الصورة\n\nستُعاد توليد الصورة آليًا.\n", encoding="utf-8")
    (story / "17_status.md").write_text("# الحالة\n\n- الحالة الحالية: `regenerating`\n", encoding="utf-8")
    run([sys.executable, str(ROOT / "scripts" / "pipeline.py"), "all", str(story)], "cloud-regenerate.log")
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    review_id = os.environ.get("GITHUB_RUN_ID", "local")
    record = {"identifier": review_id, "story_path": story.relative_to(ROOT).as_posix(), "title": metadata["title"], "video_duration_seconds": metadata.get("video_duration_seconds", 0), "review_url": os.environ.get("RUN_URL", ""), "status": "READY_FOR_APPROVAL"}
    runtime = ROOT / ".runtime"; runtime.mkdir(exist_ok=True)
    atomic_json(runtime / "review_record.json", record)
    minutes = round(float(record.get("video_duration_seconds") or 0) / 60, 1)
    keyboard = {"inline_keyboard": [
        [{"text": "🚀 اعتماد ونشر", "callback_data": f"publish:{record['identifier']}"}],
        [
            {"text": "🔄 إعادة التوليد", "callback_data": f"regenerate:{record['identifier']}"},
            {"text": "❌ رفض", "callback_data": f"reject:{record['identifier']}"},
        ],
    ]}
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["produce", "notify-review", "resume-review", "publish", "reject", "regenerate"])
    parser.add_argument("--identifier")
    args = parser.parse_args()
    if args.command == "produce":
        result = produce()
    elif args.command == "notify-review":
        result = notify_saved_review()
    elif args.command == "resume-review":
        result = resume_review()
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

