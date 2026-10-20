from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "production.yml"


def _push_branch(text: str) -> str:
    return text.split('elif [[ "$EVENT_NAME" == "push" ]]', 1)[1].split(
        'elif [[ "$EVENT_NAME" == "schedule"', 1
    )[0]


def test_resume_marker_push_remains_resume_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '".ops/resume-only-trigger.json"' in text
    assert '".ops/start-new-episode.json"' in text
    push_branch = _push_branch(text)
    assert 'grep -Fxq ".ops/start-new-episode.json"' in push_branch
    assert "python scripts/cloud_pipeline.py produce --resume-only" in push_branch


def test_start_new_episode_marker_runs_normal_production():
    text = WORKFLOW.read_text(encoding="utf-8")
    push_branch = _push_branch(text)
    start_branch = push_branch.split('grep -Fxq ".ops/start-new-episode.json"', 1)[1].split("else", 1)[0]
    assert "python scripts/cloud_pipeline.py produce" in start_branch
    assert "produce --resume-only" not in start_branch
