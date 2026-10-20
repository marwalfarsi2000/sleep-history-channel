from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import canva_thumbnail
import pipeline


def _story(tmp_path):
    story = tmp_path / "story"
    story.mkdir()
    prompt = "A calm historical market street with warm lanterns and natural empty space."
    (story / "08_image_prompt_en.txt").write_text(prompt, encoding="utf-8")
    background = story / "10_background.png"
    thumbnail = story / "11_thumbnail_base.jpg"
    background.write_bytes(b"B" * 300_001)
    thumbnail.write_bytes(b"T" * 20_000)
    thumbnail_prompt = "A calm cinematic historical market with one focal point and open space for a title."
    effective = canva_thumbnail.build_background_prompt(thumbnail_prompt, prompt)
    identity = canva_thumbnail.input_identity("أسواق تدمر", effective)
    metadata = {
        "image_generation_status": "passed",
        "image_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "image_sha256": hashlib.sha256(background.read_bytes()).hexdigest(),
        "thumbnail_sha256": hashlib.sha256(thumbnail.read_bytes()).hexdigest(),
        "thumbnail_text": "أسواق تدمر",
        "thumbnail_prompt": thumbnail_prompt,
        "thumbnail_input_sha256": identity,
    }
    (story / "15_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return story


def test_background_reuses_when_canva_identity_matches(tmp_path):
    story = _story(tmp_path)
    with patch("pipeline.can_reuse_stage", return_value=False), patch("pipeline.run") as run:
        pipeline.background(story)
    run.assert_not_called()


def test_background_regenerates_thumbnail_path_when_canva_identity_is_stale(tmp_path):
    story = _story(tmp_path)
    metadata_path = story / "15_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["thumbnail_text"] = "ليلة تدمر"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    with patch("pipeline.can_reuse_stage", return_value=False), patch("pipeline.record_stage"), patch(
        "pipeline.run"
    ) as run:
        pipeline.background(story)

    run.assert_called_once()
