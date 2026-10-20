from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "production.yml"


def test_marker_push_is_resume_only_and_never_new_episode():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '".ops/resume-only-trigger.json"' in text
    assert '[[ "$EVENT_NAME" == "push" ]]' in text
    push_branch = text.split('elif [[ "$EVENT_NAME" == "push" ]]', 1)[1].split("elif", 1)[0]
    assert "python scripts/cloud_pipeline.py produce --resume-only" in push_branch
    assert "python scripts/cloud_pipeline.py produce\n" not in push_branch
