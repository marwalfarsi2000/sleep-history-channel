"""Stage manifest for selective reuse of expensive work.

Tracks which stages have been completed for each episode and their output hashes.
Enables selective reuse: if TTS config unchanged and audio hash valid, reuse audio.
If only thumbnail style changed, regenerate thumbnail/video but not TTS.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path


def compute_file_hash(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    if not path.exists():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def compute_config_hash(config: dict) -> str:
    """Compute SHA256 hash of a config dictionary."""
    text = json.dumps(config, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_manifest(story: Path) -> dict:
    """Load stage manifest for a story."""
    manifest_path = story / "logs" / "stage_manifest.json"
    if not manifest_path.exists():
        return {"version": 1, "stages": {}}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "stages": {}}


def save_manifest(story: Path, manifest: dict) -> None:
    """Save stage manifest for a story."""
    manifest_path = story / "logs" / "stage_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def record_stage(
    story: Path,
    stage_name: str,
    input_sha256: str = "",
    output_hashes: dict | None = None,
    status: str = "completed",
) -> None:
    """Record completion of a processing stage."""
    manifest = load_manifest(story)
    manifest.setdefault("stages", {})[stage_name] = {
        "stage": stage_name,
        "input_sha256": input_sha256,
        "output_hashes": output_hashes or {},
        "status": status,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save_manifest(story, manifest)


def can_reuse_stage(
    story: Path,
    stage_name: str,
    current_input_sha256: str = "",
) -> bool:
    """Check if a stage output can be reused."""
    manifest = load_manifest(story)
    stage_info = manifest.get("stages", {}).get(stage_name, {})

    if stage_info.get("status") != "completed":
        return False

    if current_input_sha256 and stage_info.get("input_sha256") != current_input_sha256:
        return False

    for output_path, expected_hash in (stage_info.get("output_hashes") or {}).items():
        actual_path = story / output_path
        if not actual_path.exists():
            return False
        actual_hash = compute_file_hash(actual_path)
        if actual_hash != expected_hash:
            return False

    return True


def mark_stage_failed(story: Path, stage_name: str) -> None:
    """Mark a stage as failed."""
    manifest = load_manifest(story)
    if stage_name in manifest.get("stages", {}):
        manifest["stages"][stage_name]["status"] = "failed"
    save_manifest(story, manifest)
