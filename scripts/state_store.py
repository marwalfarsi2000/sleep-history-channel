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
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_STATE_BRANCH = "automation-state"


def _ensure_automation_branch() -> bool:
    """Ensure automation-state branch exists remotely, creating if necessary.

    Uses git plumbing (hash-object, commit-tree, update-ref) to avoid
    modifying the working tree. Never changes current branch.
    """
    try:
        subprocess.run(["git", "config", "user.name", "sleep-history-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "marwalfarsi2000@gmail.com"], check=False)

        # Check if remote branch exists
        result = subprocess.run(
            ["git", "rev-parse", f"origin/{AUTOMATION_STATE_BRANCH}"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return True

        # Check if local branch exists
        result = subprocess.run(
            ["git", "rev-parse", AUTOMATION_STATE_BRANCH],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            # Local exists but not pushed - push it
            subprocess.run(
                ["git", "push", "-u", "origin", f"{AUTOMATION_STATE_BRANCH}:{AUTOMATION_STATE_BRANCH}"],
                check=True, capture_output=True
            )
            return True

        # Create orphan branch safely using git plumbing (no working tree modification)
        initial = {"version": 1, "episodes": {}, "indexes": {}}
        state_json = json.dumps(initial, indent=2, ensure_ascii=False)

        # Create blob for state.json
        blob_result = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            input=state_json,
            capture_output=True, text=True, check=True
        )
        blob_sha = blob_result.stdout.strip()

        # Create tree with state.json
        tree_result = subprocess.run(
            ["git", "mktree"],
            input=f"100644 blob {blob_sha}\tstate.json\n",
            capture_output=True, text=True, check=True
        )
        tree_sha = tree_result.stdout.strip()

        # Create commit with empty parent (orphan)
        commit_result = subprocess.run(
            ["git", "commit-tree", tree_sha, "-m", "Initialize automation-state branch"],
            capture_output=True, text=True, check=True
        )
        commit_sha = commit_result.stdout.strip()

        # Create local branch at this commit
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{AUTOMATION_STATE_BRANCH}", commit_sha],
            check=True
        )

        # Push to origin
        subprocess.run(
            ["git", "push", "-u", "origin", f"{AUTOMATION_STATE_BRANCH}:{AUTOMATION_STATE_BRANCH}"],
            check=True, capture_output=True
        )
        return True
    except Exception as exc:
        print(f"Warning: Could not ensure automation-state branch: {exc}", file=sys.stderr)
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
        subprocess.run(["git", "fetch", "origin", AUTOMATION_STATE_BRANCH], check=False, capture_output=True)

        try:
            result = subprocess.run(
                ["git", "show", f"origin/{AUTOMATION_STATE_BRANCH}:state.json"],
                capture_output=True, text=True, check=True
            )
            current_state = json.loads(result.stdout)
        except subprocess.CalledProcessError:
            current_state = {"version": 1, "episodes": {}, "indexes": {}}

        current_state.setdefault("episodes", {})[episode_id] = state
        state_json = json.dumps(current_state, indent=2, ensure_ascii=False)

        blob_result = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            input=state_json,
            capture_output=True, text=True, check=True
        )
        blob_sha = blob_result.stdout.strip()

        tree_result = subprocess.run(
            ["git", "mktree"],
            input=f"100644 blob {blob_sha}\tstate.json\n",
            capture_output=True, text=True, check=True
        )
        tree_sha = tree_result.stdout.strip()

        parent_sha = subprocess.run(
            ["git", "rev-parse", f"origin/{AUTOMATION_STATE_BRANCH}"],
            capture_output=True, text=True, check=True
        ).stdout.strip()

        commit_result = subprocess.run(
            ["git", "commit-tree", tree_sha, "-p", parent_sha, "-m", f"Save episode state for {episode_id}"],
            capture_output=True, text=True, check=True
        )
        commit_sha = commit_result.stdout.strip()

        for attempt in range(4):
            result = subprocess.run(
                ["git", "push", "origin", f"{commit_sha}:refs/heads/{AUTOMATION_STATE_BRANCH}"],
                capture_output=True
            )
            if result.returncode == 0:
                return True
            if attempt < 3:
                time.sleep(2 ** attempt)

        return False
    except Exception as exc:
        print(f"Warning: Could not save episode state for {episode_id}: {exc}", file=sys.stderr)
        pass

    legacy_path = ROOT / "config" / "topics_history.json"
    if legacy_path.exists():
        try:
            topics = json.loads(legacy_path.read_text(encoding="utf-8"))
            for i, topic in enumerate(topics):
                if topic.get("story_path") and episode_id in topic["story_path"]:
                    topics[i].update(state)
                    legacy_path.write_text(json.dumps(topics, ensure_ascii=False, indent=2), encoding="utf-8")
                    return True
        except Exception:
            pass
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
