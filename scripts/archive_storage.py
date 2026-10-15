from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from internetarchive import delete, get_item, upload


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
ARCHIVE_INDEX = ROOT / "config" / "archive_index.json"


def credentials() -> tuple[str, str]:
    access = os.environ.get("IAS3_ACCESS_KEY", "").strip()
    secret = os.environ.get("IAS3_SECRET_KEY", "").strip()
    if not access or not secret:
        raise RuntimeError("Internet Archive credentials are not configured")
    return access, secret


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_identifier(slug: str, created_at: str = "") -> str:
    date = (created_at or "")[:10].replace("-", "")
    if not re.fullmatch(r"\d{8}", date):
        date = __import__("datetime").date.today().strftime("%Y%m%d")
    cleaned = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
    # Telegram callback_data is limited to 64 bytes. Keep the identifier short
    # enough for the longest action prefix ("regenerate:").
    cleaned = re.sub(r"-+", "-", cleaned)[:25].rstrip("-") or "episode"
    prefix = SETTINGS.get("archive_identifier_prefix", "lan-tukmil-alqissa")
    return f"{prefix}-{date}-{cleaned}"[:80].rstrip("-")


def metadata_for_story(story: Path, identifier: str) -> dict[str, str | list[str]]:
    data = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    description = (story / "02_description.txt").read_text(encoding="utf-8").strip()
    return {
        "identifier": identifier,
        "title": data["title"],
        "creator": SETTINGS.get("archive_creator", "لن تُكمل هذه القصة"),
        "description": description,
        "mediatype": "movies",
        "collection": SETTINGS.get("archive_collection", "opensource_movies"),
        "date": str(data.get("created_at", ""))[:10],
        "language": "ara",
        "subject": ["Arabic", "sleep story", "history", "documentary"],
        "licenseurl": SETTINGS.get("archive_license_url", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
        "approval_status": "pending_approval",
        "slug": data["slug"],
    }


def story_files(story: Path) -> dict[str, str]:
    names = (
        "14_video_final.mp4",
        "11_thumbnail_base.jpg",
        "01_title.txt",
        "02_description.txt",
        "03_sources.md",
        "04_research_notes.md",
        "05_story_clean.md",
        "06_story_tts_annotated.md",
        "07_story_tts_api.md",
        "08_image_prompt_en.txt",
        "15_metadata.json",
        "16_fact_check.md",
    )
    missing = [name for name in names if not (story / name).is_file()]
    if missing:
        raise RuntimeError("Archive upload is missing files: " + ", ".join(missing))
    return {name: str(story / name) for name in names}


def public_file_url(identifier: str, filename: str) -> str:
    return "https://archive.org/download/{}/{}".format(
        urllib.parse.quote(identifier, safe=""), urllib.parse.quote(filename)
    )


def archive_metadata(identifier: str) -> dict:
    with urllib.request.urlopen(f"https://archive.org/metadata/{identifier}", timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_remote_file(
    identifier: str,
    filename: str,
    *,
    attempts: int = 40,
    delay_seconds: int = 30,
) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            payload = archive_metadata(identifier)
            files = {entry.get("name"): entry for entry in payload.get("files") or []}
            remote = files.get(filename)
            if remote and int(remote.get("size") or 0) > 0:
                return remote
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            last_error = exc

        try:
            request = urllib.request.Request(public_file_url(identifier, filename), method="HEAD")
            with urllib.request.urlopen(request, timeout=60) as response:
                remote_size = int(response.headers.get("Content-Length") or 0)
            if remote_size > 0:
                return {"size": remote_size}
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            last_error = exc

        if attempt < attempts:
            print(
                f"Internet Archive is still processing {filename}; "
                f"verification retry {attempt}/{attempts - 1} in {delay_seconds}s",
                flush=True,
            )
            time.sleep(delay_seconds)

    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(
        f"Internet Archive file is still processing after {attempts} checks{detail}"
    )


def verify_upload(identifier: str, video: Path) -> dict:
    remote = wait_for_remote_file(identifier, video.name)
    if int(remote.get("size") or 0) != video.stat().st_size:
        raise RuntimeError("Internet Archive video size differs from the local file")
    expected = sha256(video)
    remote_sha = str(remote.get("sha256") or "").lower()
    if remote_sha and remote_sha != expected:
        raise RuntimeError("Internet Archive video checksum verification failed")
    return {"size": video.stat().st_size, "sha256": expected}


def verify_existing_upload(identifier: str, filename: str) -> dict:
    remote = wait_for_remote_file(identifier, filename)
    return {
        "size": int(remote.get("size") or 0),
        "sha256": str(remote.get("sha256") or "").lower(),
    }


def update_index(record: dict) -> None:
    records = []
    if ARCHIVE_INDEX.exists():
        records = json.loads(ARCHIVE_INDEX.read_text(encoding="utf-8"))
    records = [item for item in records if item.get("identifier") != record["identifier"]]
    records.append(record)
    records.sort(key=lambda item: item.get("created_at", ""))
    ARCHIVE_INDEX.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def finalize_story(story: Path, identifier: str, verified: dict) -> dict:
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    record = {
        "identifier": identifier,
        "slug": metadata["slug"],
        "title": metadata["title"],
        "created_at": metadata.get("created_at"),
        "status": "pending_approval",
        "video_filename": "14_video_final.mp4",
        "item_url": f"https://archive.org/details/{identifier}",
        "video_url": public_file_url(identifier, "14_video_final.mp4"),
        "thumbnail_url": public_file_url(identifier, "11_thumbnail_base.jpg"),
        "video_size": verified["size"],
        "video_sha256": verified["sha256"],
        "audio_duration_seconds": metadata.get("audio_duration_seconds", 0),
        "video_duration_seconds": metadata.get("video_duration_seconds", 0),
    }
    update_index(record)
    history_path = ROOT / "config" / "topics_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    for item in history:
        if item.get("slug") == metadata["slug"]:
            item["status"] = "pending_approval"
            item["archive_identifier"] = identifier
            item["archive_url"] = record["item_url"]
            item.pop("story_path", None)
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def finalize_existing_upload(story: Path) -> dict:
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    identifier = safe_identifier(metadata["slug"], metadata.get("created_at", ""))
    verified = verify_existing_upload(identifier, "14_video_final.mp4")
    return finalize_story(story, identifier, verified)


def upload_story(story: Path) -> dict:
    access, secret = credentials()
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    identifier = safe_identifier(metadata["slug"], metadata.get("created_at", ""))
    files = story_files(story)
    responses = upload(
        identifier,
        files,
        metadata=metadata_for_story(story, identifier),
        headers={"x-archive-keep-old-version": "0", "x-archive-queue-derive": "1"},
        access_key=access,
        secret_key=secret,
        checksum=True,
        retries=10,
        retries_sleep=15,
        verbose=False,
    )
    for response in responses if isinstance(responses, list) else [responses]:
        status = getattr(response, "status_code", 200)
        if status >= 400:
            raise RuntimeError(f"Internet Archive upload failed with HTTP {status}")
    verified = verify_upload(identifier, story / "14_video_final.mp4")
    return finalize_story(story, identifier, verified)


def delete_rejected(identifier: str) -> None:
    access, secret = credentials()
    item = get_item(identifier, access_key=access, secret_key=secret)
    files = [entry["name"] for entry in (item.item_metadata.get("files") or []) if entry.get("name")]
    original = [name for name in files if not name.startswith("__")]
    if original:
        responses = delete(
            identifier,
            files=original,
            cascade_delete=True,
            access_key=access,
            secret_key=secret,
            headers={"x-archive-keep-old-version": "0"},
        )
        for response in responses if isinstance(responses, list) else [responses]:
            status = getattr(response, "status_code", 200)
            if status >= 400:
                raise RuntimeError(f"Internet Archive delete failed with HTTP {status}")


def set_index_status(identifier: str, status: str, **updates: object) -> None:
    records = json.loads(ARCHIVE_INDEX.read_text(encoding="utf-8")) if ARCHIVE_INDEX.exists() else []
    found = False
    for record in records:
        if record.get("identifier") == identifier:
            record["status"] = status
            record.update(updates)
            found = True
    if not found:
        raise RuntimeError(f"Archive identifier is not present in the catalogue: {identifier}")
    ARCHIVE_INDEX.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    history_path = ROOT / "config" / "topics_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    for item in history:
        if item.get("archive_identifier") == identifier:
            item["status"] = status
            item.update({key: value for key, value in updates.items() if key in {"youtube_video_id"}})
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    upload_command = sub.add_parser("upload")
    upload_command.add_argument("story", type=Path)
    delete_command = sub.add_parser("delete")
    delete_command.add_argument("identifier")
    args = parser.parse_args()
    if args.command == "upload":
        print(json.dumps(upload_story(args.story.resolve()), ensure_ascii=False))
    else:
        delete_rejected(args.identifier)
        print(json.dumps({"identifier": args.identifier, "status": "rejected_deleted"}))


if __name__ == "__main__":
    main()

