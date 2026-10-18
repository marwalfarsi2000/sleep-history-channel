"""Pink noise is generated locally; no secret, no hosting, no Windows-only paths."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import background_audio
import cloud_pipeline
import pipeline

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


@pytest.fixture(autouse=True)
def no_url(monkeypatch):
    monkeypatch.delenv("PINK_NOISE_URL", raising=False)
    monkeypatch.setattr(background_audio, "GENERATED_SECONDS", 6)  # fast; production uses 300


@needs_ffmpeg
def test_no_file_and_no_url_generates_locally(tmp_path):
    path = background_audio.ensure_background_audio(tmp_path, "assets/audio/pink-noise.m4a")
    assert path == tmp_path / "assets" / "audio" / "pink-noise.m4a"
    assert background_audio.is_valid(path) and path.stat().st_size > 10_000


@needs_ffmpeg
def test_valid_existing_file_is_used_untouched(tmp_path):
    first = background_audio.ensure_background_audio(tmp_path, None)
    stamp = first.stat().st_mtime_ns
    with mock.patch.object(background_audio, "generate_locally") as generate:
        assert background_audio.ensure_background_audio(tmp_path, None) == first
    generate.assert_not_called()
    assert first.stat().st_mtime_ns == stamp


@needs_ffmpeg
@pytest.mark.parametrize("content", [b"", b"x" * 10, b"not audio at all " * 200])
def test_empty_or_corrupt_file_is_regenerated(tmp_path, content):
    target = tmp_path / "assets" / "audio" / "pink-noise.m4a"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    background_audio.ensure_background_audio(tmp_path, None)
    assert background_audio.is_valid(target)


@needs_ffmpeg
def test_external_url_is_never_needed_and_a_broken_one_cannot_stop_production(tmp_path, monkeypatch):
    monkeypatch.setenv("PINK_NOISE_URL", "https://broken.invalid/pink.m4a")
    with mock.patch("urllib.request.urlopen", side_effect=OSError("HTTP 500")) as opened:
        path = background_audio.ensure_background_audio(tmp_path, None)
    opened.assert_not_called()
    assert background_audio.is_valid(path)


@needs_ffmpeg
def test_legacy_url_is_only_a_fallback_when_local_generation_fails(tmp_path, monkeypatch):
    donor = background_audio.ensure_background_audio(tmp_path / "donor", None)
    monkeypatch.setenv("PINK_NOISE_URL", "https://example.invalid/pink.m4a")
    payload = donor.read_bytes()
    with mock.patch.object(background_audio, "generate_locally", side_effect=RuntimeError("ffmpeg missing")), \
            mock.patch("urllib.request.urlopen", return_value=io.BytesIO(payload)):
        path = background_audio.ensure_background_audio(tmp_path / "prod", None)
    assert background_audio.is_valid(path)


def test_both_paths_failing_gives_one_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PINK_NOISE_URL", "https://example.invalid/pink.m4a")
    with mock.patch.object(background_audio, "generate_locally", side_effect=RuntimeError("no ffmpeg")), \
            mock.patch("urllib.request.urlopen", side_effect=OSError("HTTP 500")):
        with pytest.raises(RuntimeError, match="Background audio unavailable"):
            background_audio.ensure_background_audio(tmp_path, None)
    assert not list((tmp_path / "assets" / "audio").glob("*"))        # no half-written file left behind


def test_windows_separators_resolve_on_linux(tmp_path):
    assert background_audio.resolve_path(tmp_path, "assets\\audio\\pink-noise.m4a") == \
        tmp_path / "assets" / "audio" / "pink-noise.m4a"
    assert background_audio.resolve_path(tmp_path, "assets/audio/pink-noise.m4a") == \
        background_audio.resolve_path(tmp_path, "assets\\audio\\pink-noise.m4a")
    assert "\\" not in (Path(__file__).resolve().parents[2] / "config" / "settings.json").read_text(
        encoding="utf-8").split('"background_audio_file"')[1].split(",")[0]


def test_cloud_pipeline_no_longer_requires_a_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_pipeline, "ROOT", tmp_path)
    with mock.patch.object(background_audio, "ensure_background_audio") as ensure:
        cloud_pipeline.ensure_background_audio()
    ensure.assert_called_once()
    assert "PINK_NOISE_URL is missing" not in Path(cloud_pipeline.__file__).read_text(encoding="utf-8")


@needs_ffmpeg
def test_final_mix_works_with_no_prebuilt_noise_file(tmp_path, monkeypatch):
    narration = tmp_path / "narration.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=3",
                    "-c:a", "libmp3lame", str(narration)], check=True)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setitem(pipeline.SETTINGS, "background_audio_file", "assets\\audio\\pink-noise.m4a")
    output = tmp_path / "mixed.mp3"
    pipeline.mix_background_audio(narration, output)
    assert output.exists() and abs(pipeline.duration(output) - 3.0) < 0.3
