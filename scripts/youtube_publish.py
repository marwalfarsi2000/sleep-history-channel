from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from archive_storage import archive_metadata


def record_remote_upload(identifier: str, video_id: str, status: str) -> None:
    access = os.environ.get("IAS3_ACCESS_KEY", "")
    secret = os.environ.get("IAS3_SECRET_KEY", "")
    if not access or not secret:
        raise RuntimeError("Internet Archive credentials are required for atomic YouTube publishing")
    from archive_storage import get_archive_item, set_index_status
    from error_classifier import retry_transient
    from state_machine import State

    canonical_state = State.PUBLISHED if status == "verified" else State.PUBLISHING
    try:
        set_index_status(identifier, canonical_state, youtube_video_id=video_id)
    except Exception as exc:
        print(f"Warning: local catalogue update for {identifier} failed: {exc}")

    def _update():
        get_archive_item(identifier, access_key=access, secret_key=secret).modify_metadata({
            "youtube_video_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "youtube_upload_status": status,
        })
    retry_transient(_update)


def wait_until_processed(youtube, video_id: str) -> dict:
    from error_classifier import classify_error, ErrorCategory
    last = {}
    for attempt in range(20):
        try:
            response = youtube.videos().list(part="status,contentDetails,snippet", id=video_id).execute()
            items = response.get("items") or []
            if items:
                last = items[0]
                upload_status = last.get("status", {}).get("uploadStatus")
                if upload_status == "processed":
                    return last
                if upload_status in {"failed", "rejected", "deleted"}:
                    raise RuntimeError(f"YouTube processing failed: {upload_status}")
        except Exception as exc:
            if classify_error(exc) != ErrorCategory.TRANSIENT_RETRYABLE:
                raise exc
        if attempt < 19:
            time.sleep(15)
    raise RuntimeError(f"YouTube processing was not verified: {last}")


def remote_url(identifier: str, filename: str) -> str:
    return "https://archive.org/download/{}/{}".format(
        urllib.parse.quote(identifier, safe=""), urllib.parse.quote(filename)
    )


def download(identifier: str, filename: str, path: Path) -> None:
    with urllib.request.urlopen(remote_url(identifier, filename), timeout=600) as response, path.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)


def read_text(identifier: str, filename: str) -> str:
    with urllib.request.urlopen(remote_url(identifier, filename), timeout=60) as response:
        return response.read().decode("utf-8-sig").strip()


def credentials() -> Credentials:
    required = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing YouTube credential fields: " + ", ".join(missing))
    return Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )


def pending_youtube_record_path(identifier: str) -> Path:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    return runtime / f"pending_youtube_{identifier}.json"


def get_pending_youtube_id(identifier: str) -> str:
    path = pending_youtube_record_path(identifier)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("youtube_video_id", "")).strip()
        except Exception:
            pass
    return ""


def save_pending_youtube_id(identifier: str, video_id: str) -> None:
    path = pending_youtube_record_path(identifier)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"youtube_video_id": video_id}, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def publish_archive_item(identifier: str) -> dict:
    payload = archive_metadata(identifier)
    metadata = payload.get("metadata") or {}

    # Check remote IA metadata, pending file, and catalogue files
    existing = metadata.get("youtube_video_id") or get_pending_youtube_id(identifier)
    if not existing:
        root = Path(__file__).resolve().parents[1]
        for cfg_name in ("topics_history.json", "archive_index.json"):
            cfg_file = root / "config" / cfg_name
            if cfg_file.exists():
                try:
                    data = json.loads(cfg_file.read_text(encoding="utf-8"))
                    for rec in data if isinstance(data, list) else []:
                        if (rec.get("identifier") == identifier or rec.get("archive_identifier") == identifier or rec.get("slug") in identifier) and rec.get("youtube_video_id"):
                            existing = rec.get("youtube_video_id")
                            break
                except Exception:
                    pass
            if existing:
                break

    youtube = build("youtube", "v3", credentials=credentials(), cache_discovery=False)

    if existing:
        try:
            verified = wait_until_processed(youtube, existing)
            if verified.get("status", {}).get("privacyStatus") == "public":
                record_remote_upload(identifier, existing, "verified")
                return {
                    "video_id": existing,
                    "url": f"https://www.youtube.com/watch?v={existing}",
                    "already_published": True,
                    "verified": verified,
                }
        except Exception as exc:
            print(f"Warning: verification of existing YouTube ID {existing} failed: {exc}")

    title = read_text(identifier, "01_title.txt")
    description = read_text(identifier, "02_description.txt")
    episode_metadata = json.loads(read_text(identifier, "15_metadata.json"))
    if episode_metadata.get("youtube_packaging_status"):
        expected_title = str(episode_metadata.get("final_youtube_title", "")).strip()
        thumbnail_text = str(episode_metadata.get("thumbnail_text", "")).strip()
        if episode_metadata.get("youtube_packaging_validation_status") != "passed":
            raise RuntimeError("YOUTUBE_PUBLISH_BLOCKED: packaging validation did not pass")
        if title != expected_title or title != str(episode_metadata.get("title", "")).strip():
            raise RuntimeError("YOUTUBE_PUBLISH_BLOCKED: approved title fields do not match")
        if not 2 <= len(re.findall(r"[\u0621-\u064A]+", thumbnail_text)) <= 4:
            raise RuntimeError("YOUTUBE_PUBLISH_BLOCKED: thumbnail text is invalid")
        if episode_metadata.get("thumbnail_text_render_status") != "passed":
            raise RuntimeError("YOUTUBE_PUBLISH_BLOCKED: exact Arabic thumbnail text was not rendered")

    # Hard-crash recovery: search YouTube directly for an existing video uploaded with this exact title
    if not existing:
        try:
            search_response = youtube.search().list(
                part="id,snippet",
                forMine=True,
                type="video",
                q=title,
                maxResults=5,
            ).execute()
            for item in search_response.get("items") or []:
                item_title = item.get("snippet", {}).get("title", "").strip()
                item_id = item.get("id", {}).get("videoId", "")
                if item_id and item_title == title.strip():
                    try:
                        verified = wait_until_processed(youtube, item_id)
                        if verified.get("status", {}).get("privacyStatus") == "public":
                            record_remote_upload(identifier, item_id, "verified")
                            return {
                                "video_id": item_id,
                                "url": f"https://www.youtube.com/watch?v={item_id}",
                                "already_published": True,
                                "verified": verified,
                            }
                    except Exception as verify_exc:
                        print(f"Warning: verification of discovered YouTube video {item_id} failed: {verify_exc}")
        except Exception as search_exc:
            print(f"Warning: YouTube search discovery failed: {search_exc}")

    with tempfile.TemporaryDirectory(prefix="sleep-history-youtube-") as folder:
        directory = Path(folder)
        video = directory / "video.mp4"
        thumbnail = directory / "thumbnail.jpg"
        download(identifier, "14_video_final.mp4", video)
        download(identifier, "11_thumbnail_base.jpg", thumbnail)
        if video.stat().st_size <= 1024:
            raise RuntimeError("Downloaded archive video is empty")

        # Step 1: Upload as PRIVATE to verify processing before public release
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title, "description": description, "categoryId": "27", "defaultLanguage": "ar"},
                "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
            },
            media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024),
        )
        response = None
        while response is None:
            _, response = request.next_chunk()
        video_id = response.get("id")
        if not video_id:
            raise RuntimeError("YouTube upload finished without a video ID")
        save_pending_youtube_id(identifier, video_id)
        record_remote_upload(identifier, video_id, "processing")

        # Step 2: Set thumbnail while processing
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg"),
        ).execute()

        # Step 3: Wait for verification while PRIVATE
        verified = wait_until_processed(youtube, video_id)
        if verified.get("status", {}).get("uploadStatus") != "processed":
            raise RuntimeError("YouTube upload processing failed or did not complete")

        # Step 4: Change to PUBLIC only after verification
        try:
            youtube.videos().update(
                part="status",
                body={
                    "id": video_id,
                    "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
                },
            ).execute()
            record_remote_upload(identifier, video_id, "verified")
            return {"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "already_published": False, "verified": verified}
        except HttpError as exc:
            if exc.resp.status in (403, 401):
                save_pending_youtube_id(identifier, video_id)
                record_remote_upload(identifier, video_id, "processing")
                return {
                    "video_id": video_id,
                    "requires_owner_action": True,
                    "action": "CHANGE_PRIVACY_TO_PUBLIC",
                    "reason": f"OAuth scope insufficient for privacy change: {exc.resp.reason}",
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "status": "private_pending_owner",
                }
            raise

