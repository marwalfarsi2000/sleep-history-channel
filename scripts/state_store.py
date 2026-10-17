"""Lightweight persistent runtime state management for episode automation.

This module handles durable state storage for episodes across retries and
checkpoint restores. It supports both the automation-state branch (preferred)
and fallback to legacy config files for backward compatibility.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_STATE_BRANCH = "automation-state"


def _ensure_automation_branch() -> bool:
    """Ensure automation-state branch exists, creating if necessary."""
    try:
        result = subprocess.run(
            ["git", "branch", "-a"],
            capture_output=True, text=True, check=True
        )
        if f"  {AUTOMATION_STATE_BRANCH}" in result.stdout or f"  remotes/origin/{AUTOMATION_STATE_BRANCH}" in result.stdout:
            return True
        subprocess.run(
            ["git", "checkout", "--orphan", AUTOMATION_STATE_BRANCH],
            check=False
        )
        subprocess.run(["git", "rm", "-rf", "."], check=False)
        initial = {"version": 1, "episodes": {}, "indexes": {}}
        subprocess.run(["git", "config", "user.name", "sleep-history-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "marwalfarsi2000@gmail.com"], check=False)
        Path("state.json").write_text(json.dumps(initial, indent=2), encoding="utf-8")
        subprocess.run(["git", "add", "state.json"], check=False)
        subprocess.run(
            ["git", "commit", "-m", "Initialize automation-state branch"],
            check=False
        )
        return True
    except Exception:
        return False


def load_episode_state(episode_id: str) -> dict:
    """Load episode state from automation-state or legacy config."""
    try:
        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        if current_branch == AUTOMATION_STATE_BRANCH:
            state_file = ROOT / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                return state.get("episodes", {}).get(episode_id, {})
    except Exception:
        pass
    legacy_path = ROOT / "config" / "topics_history.json"
    if legacy_path.exists():
        topics = json.loads(legacy_path.read_text(encoding="utf-8"))
        for topic in topics:
            if topic.get("story_path") and episode_id in topic["story_path"]:
                return topic
    return {}


def save_episode_state(episode_id: str, state: dict) -> bool:
    """Save episode state durably. Prefers automation-state, falls back to legacy."""
    try:
        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        if current_branch == AUTOMATION_STATE_BRANCH:
            state_file = ROOT / "state.json"
            if not state_file.exists():
                initial = {"version": 1, "episodes": {}, "indexes": {}}
            else:
                initial = json.loads(state_file.read_text(encoding="utf-8"))
            initial.setdefault("episodes", {})[episode_id] = state
            state_file.write_text(json.dumps(initial, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
    except Exception:
        pass
    legacy_path = ROOT / "config" / "topics_history.json"
    if legacy_path.exists():
        topics = json.loads(legacy_path.read_text(encoding="utf-8"))
        for i, topic in enumerate(topics):
            if topic.get("story_path") and episode_id in topic["story_path"]:
                topics[i].update(state)
                legacy_path.write_text(json.dumps(topics, ensure_ascii=False, indent=2), encoding="utf-8")
                return True
    return False


def load_archive_index() -> list[dict]:
    """Load archive index from automation-state or legacy config."""
    try:
        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        if current_branch == AUTOMATION_STATE_BRANCH:
            state_file = ROOT / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                return state.get("indexes", {}).get("archive", [])
    except Exception:
        pass
    legacy_path = ROOT / "config" / "archive_index.json"
    if legacy_path.exists():
        return json.loads(legacy_path.read_text(encoding="utf-8"))
    return []


def save_archive_index(index: list[dict]) -> bool:
    """Save archive index durably."""
    try:
        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        if current_branch == AUTOMATION_STATE_BRANCH:
            state_file = ROOT / "state.json"
            if not state_file.exists():
                initial = {"version": 1, "episodes": {}, "indexes": {}}
            else:
                initial = json.loads(state_file.read_text(encoding="utf-8"))
            initial.setdefault("indexes", {})["archive"] = index
            state_file.write_text(json.dumps(initial, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
    except Exception:
        pass
    legacy_path = ROOT / "config" / "archive_index.json"
    try:
        legacy_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_topics_history() -> list[dict]:
    """Load topics history from legacy config."""
    legacy_path = ROOT / "config" / "topics_history.json"
    if legacy_path.exists():
        return json.loads(legacy_path.read_text(encoding="utf-8"))
    return []


def save_topics_history(history: list[dict]) -> bool:
    """Save topics history durably."""
    legacy_path = ROOT / "config" / "topics_history.json"
    try:
        legacy_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def compute_rules_sha(rules_path: Path) -> str:
    """Compute SHA256 hash of rules file for cache invalidation."""
    if not rules_path.exists():
        return ""
    content = rules_path.read_text(encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
