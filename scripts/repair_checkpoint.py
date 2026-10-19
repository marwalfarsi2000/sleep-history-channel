"""Durable, resumable media checkpoint for the targeted review repair.

Binary outputs (background, thumbnail, TTS chunks, narration, mixed audio, video) live in immutable GitHub
Actions artifacts, one snapshot per completed expensive step. A small JSON manifest names every output with
its input fingerprint and SHA-256; a compact index goes to automation-state (never media). A fresh runner
restores the newest compatible snapshots, verifies every file individually, and only missing or stale outputs
are produced again.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Protocol

VERSION = 1
RETENTION_DAYS = 30
SNAPSHOT_PREFIX = "repair-media"
INDEX_PREFIX = "repair-checkpoint--"  # flat automation-state file name; no "/" allowed there
MANIFEST_NAME = "manifest.json"

BACKGROUND_FILE = "10_background.png"
THUMBNAIL_FILE = "11_thumbnail_base.jpg"
NARRATION_FILE = "12_audio_narration.mp3"
MIXED_FILE = "12_audio_full.mp3"
OPUS_FILE = "13_audio_review.opus"
VIDEO_FILE = "14_video_final.mp4"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(*parts: object) -> str:
    return sha256_text(json.dumps(list(parts), ensure_ascii=False, sort_keys=True, default=str))


def chunk_file(index: int) -> str:
    return f"audio_chunks/{index:03}.mp3"


def repair_hash(slug: str, identifier: str, tts_input_hash: str) -> str:
    return fingerprint(VERSION, slug, identifier, tts_input_hash)


def snapshot_prefix(slug: str, repair_hash_value: str) -> str:
    return f"{SNAPSHOT_PREFIX}--{slug}--{repair_hash_value[:16]}--"


def snapshot_name(slug: str, repair_hash_value: str, run_id: str, sequence: int) -> str:
    return f"{snapshot_prefix(slug, repair_hash_value)}run-{run_id}--step-{sequence:03}"


def new_manifest(slug: str, identifier: str, tts_input_hash: str, voice_identity: str, prompt_hash: str, total: int) -> dict:
    return {
        "version": VERSION,
        "episode_slug": slug,
        "archive_identifier": identifier,
        "repair_hash": repair_hash(slug, identifier, tts_input_hash),
        "tts_input_hash": tts_input_hash,
        "tts_voice_identity": voice_identity,
        "background_prompt_hash": prompt_hash,
        "sequence": 0,
        "decision": None,
        "background_complete": False, "background_sha256": None, "background_fp": None, "background_artifact": None,
        "metadata_patch": {},
        "thumbnail_complete": False, "thumbnail_sha256": None, "thumbnail_fp": None, "thumbnail_artifact": None,
        "tts_chunks_total": total,
        "tts_chunks": {},
        "narration_complete": False, "narration_sha256": None, "narration_fp": None, "narration_artifact": None,
        "mixed_audio_complete": False, "mixed_audio_sha256": None, "review_opus_sha256": None,
        "mixed_audio_fp": None, "mixed_audio_artifact": None,
        "video_complete": False, "video_sha256": None, "video_fp": None, "video_artifact": None, "video_extra": {},
        "archive_uploaded": False, "telegram_notified": False,
        "waiting_reason": None, "updated_at": None,
    }


# --- verification and dependency invalidation -------------------------------------------------------------

def _drop_files(story: Path, files: list[str]) -> None:
    for relative in files:
        try:
            (story / relative).unlink()
        except OSError:
            pass


def _verified(story: Path, relative: str, expected: str | None) -> bool:
    path = story / relative
    return bool(expected) and path.is_file() and sha256_file(path) == expected


def invalidate(manifest: dict, story: Path, stage: str, reason: str, log: list[str], index: int | None = None) -> None:
    """Drop one output and everything that depends on it (the minimum cascade)."""
    log.append(f"{stage}{'' if index is None else f'[{index}]'}:{reason}")
    if stage == "background":
        _drop_files(story, [BACKGROUND_FILE])
        manifest.update(background_complete=False, background_sha256=None, background_fp=None,
                        background_artifact=None, metadata_patch={})
        invalidate(manifest, story, "thumbnail", "background changed", log)
        invalidate(manifest, story, "video", "background changed", log)
    elif stage == "thumbnail":
        _drop_files(story, [THUMBNAIL_FILE])
        manifest.update(thumbnail_complete=False, thumbnail_sha256=None, thumbnail_fp=None, thumbnail_artifact=None)
    elif stage == "chunk":
        entry = manifest["tts_chunks"].pop(str(index), None)
        if entry:
            _drop_files(story, [entry.get("file") or chunk_file(index)])
        invalidate(manifest, story, "narration", f"chunk {index} changed", log)
    elif stage == "narration":
        _drop_files(story, [NARRATION_FILE])
        manifest.update(narration_complete=False, narration_sha256=None, narration_fp=None, narration_artifact=None)
        invalidate(manifest, story, "mixed", "narration changed", log)
    elif stage == "mixed":
        _drop_files(story, [MIXED_FILE, OPUS_FILE])
        manifest.update(mixed_audio_complete=False, mixed_audio_sha256=None, review_opus_sha256=None,
                        mixed_audio_fp=None, mixed_audio_artifact=None)
        invalidate(manifest, story, "video", "audio changed", log)
    elif stage == "video":
        # 15_metadata.json is shared package state, not a derived media file: keep it (package() rewrites it)
        _drop_files(story, [VIDEO_FILE, *(r for r in manifest.get("video_extra", {}) if r != "15_metadata.json")])
        manifest.update(video_complete=False, video_sha256=None, video_fp=None, video_artifact=None,
                        video_extra={}, archive_uploaded=False, telegram_notified=False)


def reconcile(manifest: dict, story: Path, expected: dict) -> list[str]:
    """Verify every recorded output against current inputs and its SHA-256; invalidate what does not match.

    ``expected`` carries: ``background_fp``, ``thumbnail_identity``, ``chunk_fps`` ({index: fp}),
    ``total``, ``mix_identity``, ``render_identity``.
    """
    log: list[str] = []
    if manifest.get("background_complete"):
        if manifest.get("background_fp") != expected["background_fp"]:
            invalidate(manifest, story, "background", "prompt or image settings changed", log)
        elif not _verified(story, BACKGROUND_FILE, manifest.get("background_sha256")):
            invalidate(manifest, story, "background", "missing or corrupt file", log)
    if manifest.get("thumbnail_complete"):
        wanted = fingerprint(manifest.get("background_sha256"), expected["thumbnail_identity"])
        if manifest.get("thumbnail_fp") != wanted:
            invalidate(manifest, story, "thumbnail", "background or renderer changed", log)
        elif not _verified(story, THUMBNAIL_FILE, manifest.get("thumbnail_sha256")):
            invalidate(manifest, story, "thumbnail", "missing or corrupt file", log)
    manifest["tts_chunks_total"] = expected["total"]
    for key in sorted(manifest["tts_chunks"], key=int):
        index = int(key)
        entry = manifest["tts_chunks"].get(key)
        if entry is None:
            continue
        if index > expected["total"] or entry.get("fp") != expected["chunk_fps"].get(index):
            invalidate(manifest, story, "chunk", "text or voice settings changed", log, index)
        elif not entry.get("complete") or not _verified(story, entry.get("file") or chunk_file(index), entry.get("sha256")):
            invalidate(manifest, story, "chunk", "missing or corrupt file", log, index)
    complete_chunks = all(str(i) in manifest["tts_chunks"] for i in range(1, expected["total"] + 1))
    if manifest.get("narration_complete"):
        wanted = narration_fingerprint(manifest)
        if not complete_chunks or manifest.get("narration_fp") != wanted:
            invalidate(manifest, story, "narration", "chunks changed", log)
        elif not _verified(story, NARRATION_FILE, manifest.get("narration_sha256")):
            invalidate(manifest, story, "narration", "missing or corrupt file", log)
    if manifest.get("mixed_audio_complete"):
        wanted = fingerprint(manifest.get("narration_sha256"), expected["mix_identity"])
        if manifest.get("mixed_audio_fp") != wanted:
            invalidate(manifest, story, "mixed", "narration or background volume changed", log)
        elif not (_verified(story, MIXED_FILE, manifest.get("mixed_audio_sha256"))
                  and _verified(story, OPUS_FILE, manifest.get("review_opus_sha256"))):
            invalidate(manifest, story, "mixed", "missing or corrupt file", log)
    if manifest.get("video_complete"):
        wanted = fingerprint(manifest.get("background_sha256"), manifest.get("mixed_audio_sha256"), expected["render_identity"])
        if manifest.get("video_fp") != wanted:
            invalidate(manifest, story, "video", "inputs or render settings changed", log)
        elif not (_verified(story, VIDEO_FILE, manifest.get("video_sha256"))
                  and all(_verified(story, rel, sha) for rel, sha in manifest.get("video_extra", {}).items())):
            invalidate(manifest, story, "video", "missing or corrupt file", log)
    return log


def narration_fingerprint(manifest: dict) -> str:
    return fingerprint([manifest["tts_chunks"][key].get("sha256") for key in sorted(manifest["tts_chunks"], key=int)])


def completed_chunks(manifest: dict) -> list[int]:
    return sorted(int(key) for key, entry in manifest["tts_chunks"].items() if entry.get("complete"))


def next_unit(manifest: dict, dry_run: bool = False) -> str:
    if not manifest["background_complete"]:
        return "background"
    if not manifest["thumbnail_complete"]:
        return "thumbnail"
    for index in range(1, manifest["tts_chunks_total"] + 1):
        if str(index) not in manifest["tts_chunks"]:
            return f"chunk:{index}"
    if not manifest["narration_complete"]:
        return "narration"
    if not manifest["mixed_audio_complete"]:
        return "mixed"
    if not manifest["video_complete"]:
        return "video"
    if dry_run:
        return "done"
    if not (manifest["archive_uploaded"] and manifest["telegram_notified"]):
        return "publish"
    return "done"


# --- artifact storage --------------------------------------------------------------------------------------

class ArtifactStore(Protocol):
    def list_snapshots(self, prefix: str) -> list[dict]: ...
    def download(self, name: str, destination: Path) -> bool: ...


class GhArtifactStore:
    """GitHub Actions artifacts through the preinstalled ``gh`` CLI (handles the signed-URL redirect)."""

    def __init__(self, repository: str | None = None):
        self.repository = repository or os.environ["GITHUB_REPOSITORY"]
        self._ids: dict[str, int] = {}

    def list_snapshots(self, prefix: str) -> list[dict]:
        found: list[dict] = []
        page = 1
        while page <= 10:
            result = subprocess.run(
                ["gh", "api", f"repos/{self.repository}/actions/artifacts?per_page=100&page={page}"],
                capture_output=True, text=True, check=False)
            if result.returncode:
                break
            artifacts = json.loads(result.stdout).get("artifacts", [])
            if not artifacts:
                break
            found += [a for a in artifacts if a["name"].startswith(prefix) and not a.get("expired")]
            page += 1
        found.sort(key=lambda a: a["created_at"], reverse=True)
        self._ids = {a["name"]: a["id"] for a in found}
        return [{"name": a["name"], "id": a["id"], "created_at": a["created_at"]} for a in found]

    def download(self, name: str, destination: Path) -> bool:
        artifact_id = self._ids.get(name)
        if artifact_id is None:
            return False
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "snapshot.zip"
            with archive.open("wb") as handle:
                result = subprocess.run(
                    ["gh", "api", f"repos/{self.repository}/actions/artifacts/{artifact_id}/zip"],
                    stdout=handle, check=False)
            if result.returncode:
                return False
            try:
                with zipfile.ZipFile(archive) as bundle:
                    for member in bundle.namelist():
                        target = (destination / member).resolve()
                        if destination.resolve() not in target.parents and target != destination.resolve():
                            return False  # path traversal in an untrusted archive
                    bundle.extractall(destination)
            except zipfile.BadZipFile:
                return False
        return True


class DirArtifactStore:
    """Local stand-in used by tests: ``<root>/<snapshot name>/...`` ; newest = lexicographically last."""

    def __init__(self, root: Path):
        self.root = root

    def publish(self, name: str, source: Path) -> None:
        target = self.root / name
        if target.exists():
            raise FileExistsError(name)  # artifacts are immutable and names are unique per run
        shutil.copytree(source, target)

    def list_snapshots(self, prefix: str) -> list[dict]:
        names = sorted((p.name for p in self.root.iterdir() if p.is_dir() and p.name.startswith(prefix)), reverse=True) \
            if self.root.exists() else []
        return [{"name": name, "id": index, "created_at": name} for index, name in enumerate(names)]

    def download(self, name: str, destination: Path) -> bool:
        source = self.root / name
        if not source.is_dir():
            return False
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return True


def _entry_artifacts(manifest: dict) -> list[tuple[list[str], str | None]]:
    items: list[tuple[list[str], str | None]] = []
    if manifest.get("background_complete"):
        items.append(([BACKGROUND_FILE], manifest.get("background_artifact")))
    if manifest.get("thumbnail_complete"):
        items.append(([THUMBNAIL_FILE], manifest.get("thumbnail_artifact")))
    for key, entry in manifest.get("tts_chunks", {}).items():
        if entry.get("complete"):
            items.append(([entry.get("file") or chunk_file(int(key))], entry.get("artifact")))
    if manifest.get("narration_complete"):
        items.append(([NARRATION_FILE], manifest.get("narration_artifact")))
    if manifest.get("mixed_audio_complete"):
        items.append(([MIXED_FILE, OPUS_FILE], manifest.get("mixed_audio_artifact")))
    if manifest.get("video_complete"):
        items.append(([VIDEO_FILE, *manifest.get("video_extra", {})], manifest.get("video_artifact")))
    return items


def compatible(manifest: object, slug: str, identifier: str, repair_hash_value: str) -> bool:
    return (isinstance(manifest, dict) and manifest.get("version") == VERSION
            and manifest.get("episode_slug") == slug and manifest.get("archive_identifier") == identifier
            and manifest.get("repair_hash") == repair_hash_value)


def restore(store: ArtifactStore, slug: str, identifier: str, repair_hash_value: str, story: Path) -> dict | None:
    """Newest compatible snapshot's manifest + the files it references. Restored data is untrusted until
    ``reconcile`` verifies every SHA-256 and input fingerprint."""
    prefix = snapshot_prefix(slug, repair_hash_value)
    snapshots = store.list_snapshots(prefix)
    chosen: dict | None = None
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        cache: dict[str, Path] = {}

        def fetch(name: str) -> Path | None:
            if name in cache:
                return cache[name]
            target = work / name
            if store.download(name, target):
                cache[name] = target
                return target
            return None

        for snapshot in snapshots:
            directory = fetch(snapshot["name"])
            if not directory or not (directory / MANIFEST_NAME).is_file():
                continue
            try:
                candidate = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if compatible(candidate, slug, identifier, repair_hash_value):
                chosen = candidate
                break
        if chosen is None:
            return None
        for files, artifact in _entry_artifacts(chosen):
            directory = fetch(artifact) if artifact else None
            if not directory:
                continue
            for relative in files:
                source = directory / relative
                if source.is_file() and ".." not in Path(relative).parts:
                    (story / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, story / relative)
    return chosen


def write_snapshot(manifest: dict, story: Path, files: list[str], snapshot_dir: Path) -> None:
    """Stage exactly the files produced by this step plus the cumulative manifest for upload."""
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True)
    for relative in files:
        target = snapshot_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(story / relative, target)
    manifest["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    (snapshot_dir / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def index_payload(manifest: dict, snapshot: str, run_id: str) -> dict:
    return {
        "archive_identifier": manifest["archive_identifier"],
        "repair_hash": manifest["repair_hash"],
        "latest_snapshot": snapshot,
        "completed_tts_chunks": completed_chunks(manifest),
        "tts_chunks_total": manifest["tts_chunks_total"],
        "background_complete": manifest["background_complete"],
        "narration_complete": manifest["narration_complete"],
        "mixed_audio_complete": manifest["mixed_audio_complete"],
        "video_complete": manifest["video_complete"],
        "archive_uploaded": manifest["archive_uploaded"],
        "last_run_id": run_id,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "waiting_reason": manifest.get("waiting_reason"),
    }


def save_index(slug: str, payload: dict) -> bool:
    """Small text pointer in automation-state; best effort because the artifacts are authoritative."""
    try:
        import state_store
        name = f"{INDEX_PREFIX}{slug}.json"
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return state_store._update_remote(lambda read: {name: body}, f"Repair checkpoint {slug}")
    except Exception as exc:  # noqa: BLE001 - never mask the real repair outcome
        print(f"WARNING: repair checkpoint index was not persisted: {exc}")
        return False
