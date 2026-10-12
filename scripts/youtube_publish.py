from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from archive_storage import archive_metadata


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


def publish_archive_item(identifier: str) -> dict:
    payload = archive_metadata(identifier)
    metadata = payload.get("metadata") or {}
    existing = metadata.get("youtube_video_id")
    if existing:
        return {"video_id": existing, "url": f"https://www.youtube.com/watch?v={existing}", "already_published": True}
    title = read_text(identifier, "01_title.txt")
    description = read_text(identifier, "02_description.txt")
    with tempfile.TemporaryDirectory(prefix="sleep-history-youtube-") as folder:
        directory = Path(folder)
        video = directory / "video.mp4"
        thumbnail = directory / "thumbnail.jpg"
        download(identifier, "14_video_final.mp4", video)
        download(identifier, "11_thumbnail_base.jpg", thumbnail)
        if video.stat().st_size <= 1024:
            raise RuntimeError("Downloaded archive video is empty")
        youtube = build("youtube", "v3", credentials=credentials(), cache_discovery=False)
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title, "description": description, "categoryId": "27", "defaultLanguage": "ar"},
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
            },
            media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024),
        )
        response = None
        while response is None:
            _, response = request.next_chunk()
        video_id = response.get("id")
        if not video_id:
            raise RuntimeError("YouTube upload finished without a video ID")
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg"),
        ).execute()
    return {"video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "already_published": False}

