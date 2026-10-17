import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state_store


@pytest.fixture(autouse=True)
def isolate_state_store(monkeypatch):
    """No test may reach the repository's real remote through state_store."""
    monkeypatch.setattr(state_store, "REMOTE", "state-store-test-unconfigured")
    monkeypatch.setattr(state_store.time, "sleep", lambda *_: None)
