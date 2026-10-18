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


def latest_unfinished_story(root: Path = ROOT, state_episodes: dict | None = None) -> str | None:
    """Story folder of the active episode, using the same rule as generate_daily."""
    import episode_selection

    history_path = root / "config" / "topics_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    record = episode_selection.active_episode(history, root, state_episodes)
    if not record or not record.get("story_path"):
        return None
    return PurePosixPath(str(record["story_path"]).replace("\\", "/")).as_posix()


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
        "05b_story_tts_normalized.md",
        "06_story_tts_annotated.md",
        "07_story_tts_api.md",
        "15_metadata.json",
        "17_status.md",
        "01_title.txt", "02_description.txt", "03_sources.md", "04_research_notes.md",
        "08_image_prompt_en.txt", "08_thumbnail_prompt_en.txt", "09_image_source.md", "10_background.png",
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


def _record_restore(story_path: str, source: str, artifact: dict | None = None) -> None:
    try:
        import run_summary
        run_summary.start(workflow="restore", action="restore")
        run_summary.update(
            restored_from_checkpoint=True, story_path=story_path, checkpoint_saved=True,
            checkpoint_source=source, checkpoint_location=story_path,
            checkpoint_artifact_id=(artifact or {}).get("id"), checkpoint_artifact_name=(artifact or {}).get("name"),
            checkpoint_created_at=(artifact or {}).get("created_at"),
        )
    except Exception:
        pass


def restore_durable_checkpoint(root: Path, story_path: str, *, record: bool = True) -> bool:
    """Fallback when no (unexpired) artifact matches: the small text state lives on automation-state."""
    try:
        import state_store
        restored = state_store.restore_story_checkpoint(root, PurePosixPath(story_path).name)
    except Exception as exc:
        print(f"Durable checkpoint restore unavailable: {exc}")
        return False
    if restored:
        print(f"Restored {restored} file(s) for {story_path} from the automation-state checkpoint.")
        if record:
            _record_restore(story_path, "automation-state")
    return bool(restored)


def migrate_history(root: Path) -> list[str]:
    """Retire zombie history records once (idempotent) so they stop warning on every run."""
    import episode_selection
    path = root / "config" / "topics_history.json"
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
        migrated = episode_selection.migrate_zombie_records(history, root)
        if migrated:
            path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Retired zombie history record(s) as ABANDONED_LEGACY: {migrated}")
        return migrated
    except Exception as exc:
        print(f"History migration skipped: {exc}")
        return []


def restore_latest_checkpoint(root: Path = ROOT) -> bool:
    migrate_history(root)
    try:
        import state_store
        state_episodes = state_store._load_state().get("episodes", {})
    except Exception:
        state_episodes = {}
    story_path = latest_unfinished_story(root, state_episodes)
    if not story_path:
        print("Checkpoint restore skipped: no unfinished story.")
        return False
    from_artifact = restore_from_artifact(root, story_path)
    # An expired or missing artifact must never make the episode unrecoverable; the durable
    # copy also fills gaps (it never overwrites a file the artifact restored).
    from_state = restore_durable_checkpoint(root, story_path, record=not from_artifact)
    return from_artifact or from_state


def restore_from_artifact(root: Path, story_path: str) -> bool:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repository:
        print("Artifact restore skipped: GitHub runner credentials unavailable.")
        return False
    try:
        data = github_json(
            f"https://api.github.com/repos/{repository}/actions/artifacts?per_page=100",
            token,
        )
    except Exception as exc:
        print(f"Artifact listing failed ({type(exc).__name__}); trying the durable checkpoint.")
        return False
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
        try:
            payload = download(artifact["archive_download_url"], token)
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except Exception as exc:
            print(f"Artifact {artifact.get('name')} could not be read ({type(exc).__name__}); skipping.")
            continue
        with archive:
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
            _record_restore(story_path, "artifact", artifact)
            return True
    print(f"No saved failure checkpoint matched {story_path}.")
    return False


if __name__ == "__main__":
    restore_latest_checkpoint()
