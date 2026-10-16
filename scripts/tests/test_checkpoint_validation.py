import json
import datetime as dt
from pathlib import Path
import pytest
import generate_daily


def test_checkpoint_validation_matrix(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    old_iso = "2020-01-01T00:00:00+00:00"

    history = [
        # 1. Missing created_at without folder -> invalid
        {"slug": "missing-created-at", "status": "drafting", "story_path": "stories/missing-created-at"},
        # 2. Malformed created_at without folder -> invalid
        {"slug": "malformed-created-at", "status": "drafting", "story_path": "stories/malformed-created-at", "created_at": "not-a-date"},
        # 3. Stale missing directory (>7 days) -> invalid
        {"slug": "stale-missing-dir", "status": "drafting", "story_path": "stories/stale-missing-dir", "created_at": old_iso},
        # 4. Recent missing directory (<7 days) -> valid topic resume candidate
        {"slug": "recent-missing-dir", "status": "drafting", "story_path": "stories/recent-missing-dir", "created_at": now_iso},
        # 5. Valid existing checkpoint with folder & metadata -> valid
        {"slug": "valid-existing", "status": "drafting", "story_path": "stories/valid-existing", "created_at": now_iso},
    ]

    # Create directory and metadata for valid-existing
    valid_dir = tmp_path / "stories" / "valid-existing"
    valid_dir.mkdir(parents=True)
    (valid_dir / "15_metadata.json").write_text("{}", encoding="utf-8")

    history_file = config_dir / "topics_history.json"
    history_file.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)

    # Calling create_daily_story should select "valid-existing" or "recent-missing-dir" instead of the stale/malformed ones
    from unittest.mock import MagicMock
    mock_resume = MagicMock()
    monkeypatch.setattr(generate_daily, "resume_partial_story", mock_resume)

    generate_daily.create_daily_story()

    # Should have selected the valid existing checkpoint or recent recoverable
    assert mock_resume.called
    selected_path = mock_resume.call_args[0][0]
    assert "valid-existing" in str(selected_path) or "recent-missing-dir" in str(selected_path)
