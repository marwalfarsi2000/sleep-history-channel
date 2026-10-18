"""Pink-noise bed for the final mix: bundled file, else generated locally with ffmpeg.

``PINK_NOISE_URL`` is only a legacy last resort; production needs no secret and no hosting.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

DEFAULT_FILE = "assets/audio/pink-noise.m4a"
MIN_BYTES = 1024
GENERATED_SECONDS = 300  # the mix loops it (-stream_loop -1) to the narration length
FFMPEG_TIMEOUT = 300


def resolve_path(root: Path, configured: str | None) -> Path:
    """A path written with Windows or POSIX separators, resolved under ``root`` on any OS."""
    parts = [part for part in re.split(r"[\\/]+", (configured or DEFAULT_FILE).strip()) if part]
    return root.joinpath(*parts)


def _probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return float(GENERATED_SECONDS) if path.stat().st_size > MIN_BYTES else 0.0
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def is_valid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > MIN_BYTES and _probe_duration(path) > 1.0
    except (OSError, subprocess.SubprocessError):
        return False


def generate_locally(target: Path, seconds: int | None = None) -> None:
    seconds = seconds or GENERATED_SECONDS
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; cannot generate pink noise locally")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part.m4a")
    command = [
        ffmpeg, "-y", "-v", "error", "-f", "lavfi",
        "-i", f"anoisesrc=color=pink:duration={seconds}:sample_rate=44100:amplitude=0.5:seed=7",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT)
        if not is_valid(temporary):
            raise RuntimeError("generated pink noise is empty or unreadable")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def download_legacy(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        if not is_valid(temporary):
            raise RuntimeError("Downloaded pink-noise asset is empty or unreadable")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_background_audio(root: Path, configured: str | None = None) -> Path:
    """Return a valid pink-noise file; never depends on a secret or an external host."""
    target = resolve_path(root, configured)
    if is_valid(target):
        return target
    target.unlink(missing_ok=True)  # empty or corrupt: regenerate
    try:
        generate_locally(target)
        return target
    except Exception as local_error:
        url = os.environ.get("PINK_NOISE_URL", "").strip()
        if not url:
            raise RuntimeError(f"Background audio could not be generated locally: {local_error}") from local_error
        try:
            download_legacy(url, target)
        except Exception as remote_error:
            raise RuntimeError(
                f"Background audio unavailable (local: {local_error}; legacy URL failed: {type(remote_error).__name__})"
            ) from remote_error
        return target
