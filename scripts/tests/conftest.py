import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_router
import run_summary
import state_store


@pytest.fixture(autouse=True)
def isolate_state_store(monkeypatch):
    """No test may reach the repository's real remote through state_store."""
    monkeypatch.setattr(state_store, "REMOTE", "state-store-test-unconfigured")
    monkeypatch.setattr(state_store.time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def isolate_runtime_files(monkeypatch, tmp_path):
    """Routing state and run summaries of one test must never leak into the repo or other tests."""
    monkeypatch.setattr(llm_router, "STATE_PATH", tmp_path / "llm_quota_state.json")
    monkeypatch.setattr(run_summary, "SUMMARY_PATH", tmp_path / "run_summary.json")
