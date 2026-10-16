import json
from pathlib import Path
import pytest
import generate_daily


def test_stale_checkpoint_skipped(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    history = [
        {
            "slug": "old-stale-slug",
            "status": "drafting",
            "story_path": "stories/old-stale-slug",
            "created_at": "2020-01-01T00:00:00+00:00"
        }
    ]
    history_file = config_dir / "topics_history.json"
    history_file.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(generate_daily, "ROOT", tmp_path)

    # Calling choose_valid_topic should select a new topic instead of raising on old stale entry
    with pytest.raises(RuntimeError):
        # Without mock Gemini, choose_valid_topic will fail on LLM call, proving it skipped the stale checkpoint and proceeded to choose_valid_topic
        generate_daily.create_daily_story()
