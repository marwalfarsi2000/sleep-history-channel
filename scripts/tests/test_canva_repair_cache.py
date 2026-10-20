from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import canva_thumbnail
import repair_review


def _write_metadata(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_canva_input_identity_changes_with_hook_or_prompt():
    first = canva_thumbnail.input_identity("أسواق تدمر", "historical market background")
    assert first != canva_thumbnail.input_identity("ليلة تدمر", "historical market background")
    assert first != canva_thumbnail.input_identity("أسواق تدمر", "different historical background")


def test_repair_renderer_uses_production_canva_builder(tmp_path):
    story = tmp_path / "story"
    story.mkdir()
    (story / "10_background.png").write_bytes(b"background")
    _write_metadata(
        story / "15_metadata.json",
        {"thumbnail_prompt": "calm historical market with open space"},
    )
    expected = ({"thumbnail_generation_provider": "canva_via_n8n"}, "passed")
    with patch("image_generate.build_final_thumbnail", return_value=expected) as build:
        assert repair_review.render_thumbnail_file(story, "أسواق تدمر") == expected
    build.assert_called_once_with(
        story,
        story / "10_background.png",
        "أسواق تدمر",
        "calm historical market with open space",
    )


def test_repair_reuses_current_canva_thumbnail_without_second_generation(tmp_path):
    story = tmp_path / "story"
    story.mkdir()
    thumb = story / "11_thumbnail_base.jpg"
    thumb.write_bytes(b"valid-thumbnail")
    sha = hashlib.sha256(thumb.read_bytes()).hexdigest()
    identity = "identity-123"
    _write_metadata(
        story / "15_metadata.json",
        {
            "thumbnail_generation_provider": "canva_via_n8n",
            "thumbnail_text_render_status": "passed",
            "thumbnail_input_sha256": identity,
            "thumbnail_sha256": sha,
        },
    )
    manifest = {
        "metadata_patch": {},
        "background_sha256": "background-sha",
    }
    context = SimpleNamespace(
        story=story,
        manifest=manifest,
        thumbnail_text="أسواق تدمر",
        expected=lambda: {"thumbnail_identity": identity},
    )

    with patch("repair_review.render_thumbnail_file") as render:
        repair_review.unit_thumbnail(context, "thumbnail-artifact")
    render.assert_not_called()
    assert manifest["thumbnail_complete"] is True
    assert manifest["thumbnail_sha256"] == sha


def test_repair_regenerates_when_canva_identity_is_stale(tmp_path):
    story = tmp_path / "story"
    story.mkdir()
    thumb = story / "11_thumbnail_base.jpg"
    thumb.write_bytes(b"old-thumbnail")
    old_sha = hashlib.sha256(thumb.read_bytes()).hexdigest()
    _write_metadata(
        story / "15_metadata.json",
        {
            "thumbnail_generation_provider": "canva_via_n8n",
            "thumbnail_text_render_status": "passed",
            "thumbnail_input_sha256": "old-identity",
            "thumbnail_sha256": old_sha,
        },
    )
    manifest = {"metadata_patch": {}, "background_sha256": "background-sha"}
    context = SimpleNamespace(
        story=story,
        manifest=manifest,
        thumbnail_text="أسواق تدمر",
        expected=lambda: {"thumbnail_identity": "new-identity"},
    )

    def rebuild(_story: Path, _text: str):
        thumb.write_bytes(b"new-thumbnail")
        return {
            "thumbnail_generation_provider": "canva_via_n8n",
            "thumbnail_input_sha256": "new-identity",
        }, "passed"

    with patch("repair_review.render_thumbnail_file", side_effect=rebuild) as render:
        repair_review.unit_thumbnail(context, "thumbnail-artifact")
    render.assert_called_once()
    assert manifest["metadata_patch"]["thumbnail_input_sha256"] == "new-identity"
    assert manifest["metadata_patch"]["thumbnail_sha256"] == hashlib.sha256(b"new-thumbnail").hexdigest()
