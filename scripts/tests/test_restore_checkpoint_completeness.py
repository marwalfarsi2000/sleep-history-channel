import json
import zipfile
import io
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import restore_failure_checkpoint


def test_safe_story_members_includes_tts_normalized():
    zip_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_bytes, "w") as zf:
        zf.writestr("stories/palmyra/05b_story_tts_normalized.md", "normalized content")
        zf.writestr("stories/palmyra/05_story_clean.md", "clean content")

    zip_bytes.seek(0)
    with zipfile.ZipFile(zip_bytes) as zf:
        members = restore_failure_checkpoint.safe_story_members(zf, "stories/palmyra")
        filenames = [m.filename for m in members]
        assert "stories/palmyra/05b_story_tts_normalized.md" in filenames


def test_latest_unfinished_story_skips_completed_and_review_stories(tmp_path):
    history_file = tmp_path / "config" / "topics_history.json"
    history_file.parent.mkdir(parents=True)
    history = [
        {"slug": "story1", "status": "drafting", "story_path": "stories/story1"},
        {"slug": "story2", "status": "REVIEW_PENDING", "archive_identifier": "ia-2", "story_path": "stories/story2"},
    ]
    history_file.write_text(json.dumps(history), encoding="utf-8")

    result = restore_failure_checkpoint.latest_unfinished_story(tmp_path)
    assert result == "stories/story1"
