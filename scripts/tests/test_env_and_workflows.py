"""Documentation and workflow hygiene that production reliability depends on."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_every_secret_a_workflow_uses_is_documented_in_env_example():
    documented = {line.split("=")[0] for line in read(".env.example").splitlines() if "=" in line and not line.startswith("#")}
    used = set()
    for workflow in WORKFLOWS:
        used |= set(re.findall(r"secrets\.([A-Z0-9_]+)", workflow.read_text(encoding="utf-8")))
    used.discard("GITHUB_TOKEN")
    assert used - documented == set()
    assert {"EXA_API_KEY", "BRAVE_SEARCH_API_KEY", "FIRECRAWL_API_KEY", "GOOGLE_CLOUD_TTS_API_KEY"} <= documented


def test_env_example_holds_no_values():
    for line in read(".env.example").splitlines():
        if "=" in line and not line.startswith("#"):
            name, value = line.split("=", 1)
            assert value in ("", "sleep-history-channel"), name


def test_review_workflow_has_the_same_failure_diagnostics_as_production():
    review, production = read(".github/workflows/review.yml"), read(".github/workflows/production.yml")
    for text in (review, production):
        assert "python scripts/notify_failure.py" in text
        assert ".runtime/run_summary.json" in text
        assert "retention-days: 30" in text
        assert "python scripts/git_sync.py" in text
    assert "steps.apply.outcome" in review
    assert "sleep-history-review-failure-" in review and "sleep-history-failure-" not in review.replace(
        "sleep-history-review-failure-", "")  # restore step only reads production checkpoints


def test_production_resume_only_schedules_never_start_a_new_topic():
    production = read(".github/workflows/production.yml")
    assert 'cron: "30 8 * * *"' in production                      # the one daily production pass
    extra = re.findall(r'cron: "(30 \d+ \* \* \*)"', production)
    assert len(extra) == 3
    assert 'EVENT_SCHEDULE" != "30 8 * * *"' in production
    assert "produce --resume-only" in production
    assert production.index("produce --resume-only") < production.index("python scripts/cloud_pipeline.py produce\n")
    assert 'SLEEP_HISTORY_DURABLE_CHECKPOINTS: "1"' in production


def test_workflows_keep_timeouts_concurrency_and_minimal_permissions():
    for name in ("production.yml", "review.yml"):
        text = read(f".github/workflows/{name}")
        assert "timeout-minutes: 355" in text
        assert "group: sleep-history-production" in text and "cancel-in-progress: false" in text
        assert "contents: write" in text and "actions: read" in text


def test_python_version_is_pinned_in_every_workflow_that_installs_dependencies():
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        if "pip install" in text:
            assert 'python-version: "3.13"' in text, workflow.name
