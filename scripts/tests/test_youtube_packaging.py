import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import generate_daily


def test_local_packaging_issues_flexible_prompt():
    topic = {"topic_ar": "تدمر في العصر الروماني"}
    recent = []
    package = {
        "episode_title_core": "الحياة اليومية في تدمر الرومانية",
        "title_suffix": "قصة تاريخية للنوم 😴",
        "final_youtube_title": "الحياة اليومية في تدمر الرومانية | قصة تاريخية للنوم 😴",
        "thumbnail_text": "تدمر الرومانية",
        "thumbnail_prompt": "16:9 horizontal YouTube background scene, ancient colonnaded street in Palmyra at twilight, soft moonlight, quiet evening atmosphere, dark reserved space on left side for text overlay."
    }

    issues = generate_daily.local_packaging_issues(package, topic, recent)
    assert len(issues) == 0


def test_local_packaging_issues_rejects_placeholders_and_long_prompts():
    topic = {"topic_ar": "تدمر في العصر الروماني"}
    recent = []
    package = {
        "episode_title_core": "الحياة اليومية في تدمر الرومانية",
        "title_suffix": "قصة تاريخية للنوم 😴",
        "final_youtube_title": "الحياة اليومية في تدمر الرومانية | قصة تاريخية للنوم 😴",
        "thumbnail_text": "تدمر الرومانية",
        "thumbnail_prompt": "Prompt with placeholder [EPISODE_TOPIC] inside"
    }

    issues = generate_daily.local_packaging_issues(package, topic, recent)
    assert "thumbnail_prompt" in issues

    # Prompt exceeding THUMBNAIL_PROMPT_MAX_CHARS (1700)
    package_long = dict(package)
    package_long["thumbnail_prompt"] = "A" * 1750
    issues_long = generate_daily.local_packaging_issues(package_long, topic, recent)
    assert "thumbnail_prompt" in issues_long


def test_packaging_attempts_diagnostic_logging(tmp_path):
    story_dir = tmp_path / "stories" / "test-story"
    story_dir.mkdir(parents=True)
    logs_dir = story_dir / "logs"
    logs_dir.mkdir()

    topic = {
        "topic_ar": "تدمر في العصر الروماني",
        "location_ar": "تدمر",
        "period_ar": "العصر الروماني"
    }

    bad_candidate = {
        "episode_title_core": "الحياة اليومية في تدمر الرومانية",
        "title_suffix": "قصة تاريخية للنوم 😴",
        "final_youtube_title": "الحياة اليومية في تدمر الرومانية | قصة تاريخية للنوم 😴",
        "thumbnail_text": "تدمر الرومانية",
        "thumbnail_prompt": "too short"
    }

    with patch("generate_daily.gemini", return_value=(json.dumps(bad_candidate), [])):
        with pytest.raises(RuntimeError) as exc_info:
            generate_daily.generate_youtube_packaging(story_dir, topic)
        assert "YOUTUBE_PACKAGING_VALIDATION_FAILED" in str(exc_info.value)
        assert (logs_dir / "youtube_packaging_attempts.json").exists()
