import json
import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock
import pipeline


def test_background_reuse_existing_image(tmp_path, monkeypatch):
    story_dir = tmp_path / "stories" / "test-story"
    story_dir.mkdir(parents=True)
    img = story_dir / "10_background.png"
    thumb = story_dir / "11_thumbnail_base.jpg"
    img.write_bytes(b"X" * 300_000)
    thumb.write_bytes(b"X" * 10_000)

    prompt_file = story_dir / "08_image_prompt_en.txt"
    prompt_file.write_text("Test image prompt", encoding="utf-8")
    prompt_hash = hashlib.sha256("Test image prompt".encode("utf-8")).hexdigest()

    meta_file = story_dir / "15_metadata.json"
    meta_file.write_text(json.dumps({
        "image_generation_status": "passed",
        "image_prompt_sha256": prompt_hash
    }), encoding="utf-8")

    with patch("pipeline.run") as mock_run:
        pipeline.background(story_dir, force=False)
        mock_run.assert_not_called()
