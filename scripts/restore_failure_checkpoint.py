#!/usr/bin/env python3
"""Restore the latest matching failed-run checkpoint on an ephemeral runner."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath
import urllib.request
import urllib.error
import urllib.parse
import zipfile


ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "2022-11-28"
ARTIFACT_PREFIX = "sleep-history-failure-"


def latest_unfinished_story(root: Path = ROOT) -> str | None:
    history_path = root / "config" / "topics_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    for item in reversed(history):
        status = item.get("status")
        upload_incomplete = (
            status == "pending_approval"
            and not item.get("archive_identifier")
        )
        review_incomplete = status == "READY_FOR_APPROVAL" and not item.get("review_run_id")
        if (status in {"generating", "drafting"} or upload_incomplete or review_incomplete) and item.get("story_path"):
            return PurePosixPath(str(item["story_path"]).replace("\\", "/")).as_posix()
    return None


def github_json(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "sleep-history-checkpoint-restorer",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url: str, token: str) -> bytes:
    class StopRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "sleep-history-checkpoint-restorer",
        },
    )
    # GitHub responds with a short-lived signed Azure Blob URL. Never forward
    # the repository bearer token to that different host; Azure rejects the
    # extra Authorization header and the token should remain scoped to GitHub.
    opener = urllib.request.build_opener(StopRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location", "")
        parsed = urllib.parse.urlparse(location)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("GitHub returned an invalid artifact redirect") from error
        redirected = urllib.request.Request(
            location,
            headers={"User-Agent": "sleep-history-checkpoint-restorer"},
        )
        with urllib.request.urlopen(redirected, timeout=120) as response:
            return response.read()


def safe_story_members(archive: zipfile.ZipFile, story_path: str) -> list[zipfile.ZipInfo]:
    story_prefix = PurePosixPath(story_path).as_posix().rstrip("/") + "/"
    allowed_root_files = {
        "05_story_clean.md",
        "06_story_tts_annotated.md",
        "07_story_tts_api.md",
        "15_metadata.json",
        "17_status.md",
        "01_title.txt", "02_description.txt", "03_sources.md", "04_research_notes.md",
        "08_image_prompt_en.txt", "09_image_source.md", "10_background.png",
        "11_thumbnail_base.jpg", "12_audio_narration.mp3", "12_audio_full.mp3",
        "13_audio_review.opus", "14_video_final.mp4", "16_fact_check.md", "00_package.html",
    }
    allowed_directories = {"logs", "tts_chunks", "audio_chunks"}
    members: list[zipfile.ZipInfo] = []
    for member in archive.infolist():
        normalized = PurePosixPath(member.filename).as_posix()
        if member.is_dir() or not normalized.startswith(story_prefix):
            continue
        path = PurePosixPath(normalized)
        if ".." in path.parts:
            continue
        relative = PurePosixPath(normalized[len(story_prefix):])
        if len(relative.parts) == 1:
            if relative.name not in allowed_root_files:
                continue
        elif relative.parts[0] not in allowed_directories:
            continue
        members.append(member)
    return members


def restore_latest_checkpoint(root: Path = ROOT) -> bool:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repository:
        print("Checkpoint restore skipped: GitHub runner credentials unavailable.")
        return False
    story_path = latest_unfinished_story(root)
    if not story_path:
        print("Checkpoint restore skipped: no unfinished story.")
        return False

    data = github_json(
        f"https://api.github.com/repos/{repository}/actions/artifacts?per_page=100",
        token,
    )
    artifacts = sorted(
        (
            item for item in data.get("artifacts", [])
            if str(item.get("name", "")).startswith(ARTIFACT_PREFIX)
            and not item.get("expired")
        ),
        key=lambda item: item.get("created_at", ""),
        reverse=True,
    )
    for artifact in artifacts:
        payload = download(artifact["archive_download_url"], token)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = safe_story_members(archive, story_path)
            if not members:
                continue
            for member in members:
                relative = PurePosixPath(member.filename)
                destination = root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member))
            print(
                f"Restored {len(members)} checkpoint file(s) for {story_path} "
                f"from {artifact['name']}."
            )
            return True
    print(f"No saved failure checkpoint matched {story_path}.")
    return False


if __name__ == "__main__":
    restore_latest_checkpoint()
