"""Integration tests using real git repositories (local bare origin, no network)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import cloud_pipeline
import git_sync
import state_store

REPO = Path(__file__).resolve().parents[2]


def git(cwd, *args, check=True):
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def clone(bare: Path, target: Path) -> Path:
    git(bare.parent, "clone", "-q", str(bare), str(target))
    return target


@pytest.fixture
def repos(tmp_path, monkeypatch):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "config").mkdir()
    (seed / "config" / "topics_history.json").write_text("[]\n", encoding="utf-8")
    (seed / "config" / "archive_index.json").write_text("[]\n", encoding="utf-8")
    (seed / "code.py").write_text("print(1)\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    work = clone(bare, tmp_path / "work")
    monkeypatch.setattr(state_store, "ROOT", work)
    monkeypatch.setattr(state_store, "REMOTE", "origin")
    return SimpleNamespace(bare=bare, work=work, tmp=tmp_path)


def remote_file(bare: Path, name: str):
    result = subprocess.run(
        ["git", "show", f"automation-state:{name}"], cwd=bare, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout if result.returncode == 0 else None


def main_sha(bare: Path) -> str:
    return git(bare, "rev-parse", "main")


def test_ensure_creates_remote_branch_without_touching_working_tree(repos):
    head_before = git(repos.work, "symbolic-ref", "--short", "HEAD")
    assert state_store.ensure_automation_branch() is True
    assert git(repos.bare, "rev-parse", "--verify", "automation-state")
    assert git(repos.work, "symbolic-ref", "--short", "HEAD") == head_before == "main"
    assert git(repos.work, "status", "--porcelain") == ""
    assert not (repos.work / "state.json").exists()
    assert git(repos.work, "branch", "--list", "automation-state") == ""
    assert json.loads(remote_file(repos.bare, "state.json"))["episodes"] == {}


def test_ensure_is_idempotent(repos):
    assert state_store.ensure_automation_branch()
    tip = git(repos.bare, "rev-parse", "automation-state")
    assert state_store.ensure_automation_branch()
    assert git(repos.bare, "rev-parse", "automation-state") == tip


def test_remote_state_is_read_while_current_branch_stays_code_branch(repos, tmp_path, monkeypatch):
    assert state_store.save_episode_state("ep_1", {"approval_status": "REVIEW_PENDING"})
    other = clone(repos.bare, tmp_path / "other")
    monkeypatch.setattr(state_store, "ROOT", other)
    assert state_store.load_episode_state("ep_1")["approval_status"] == "REVIEW_PENDING"
    assert git(other, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(other, "status", "--porcelain") == ""
    assert not (other / "state.json").exists()


def test_episode_state_write_targets_automation_state(repos):
    before = main_sha(repos.bare)
    assert state_store.save_episode_state("ep_1", {"approval_status": "REVIEW_PENDING"})
    assert state_store.save_episode_state("ep_1", {"youtube_video_id": "abc"})
    episodes = json.loads(remote_file(repos.bare, "state.json"))["episodes"]
    assert episodes["ep_1"] == {"approval_status": "REVIEW_PENDING", "youtube_video_id": "abc"}
    assert main_sha(repos.bare) == before
    assert git(repos.work, "symbolic-ref", "--short", "HEAD") == "main"


def test_archive_index_write_targets_automation_state(repos):
    before = main_sha(repos.bare)
    records = [{"identifier": "a", "status": "REVIEW_PENDING"}]
    assert state_store.save_archive_index(records)
    assert json.loads(remote_file(repos.bare, "archive_index.json")) == records
    assert state_store.load_archive_index() == records
    assert main_sha(repos.bare) == before


def test_topics_history_write_targets_automation_state(repos):
    before = main_sha(repos.bare)
    history = [{"slug": "x", "status": "drafting"}]
    assert state_store.save_topics_history(history)
    assert json.loads(remote_file(repos.bare, "topics_history.json")) == history
    assert state_store.load_topics_history() == history
    assert main_sha(repos.bare) == before


def test_reads_fall_back_to_legacy_files_when_branch_has_no_copy(repos):
    (repos.work / "config" / "archive_index.json").write_text('[{"identifier": "legacy"}]', encoding="utf-8")
    assert state_store.load_archive_index() == [{"identifier": "legacy"}]


def test_persist_all_writes_runtime_state_but_never_main(repos):
    before = main_sha(repos.bare)
    (repos.work / "config" / "topics_history.json").write_text('[{"slug": "s"}]', encoding="utf-8")
    (repos.work / "config" / "tts_usage.json").write_text('{"chars": 5}', encoding="utf-8")
    story = repos.work / "stories" / "2026-10-17_s"
    story.mkdir(parents=True)
    (story / "15_metadata.json").write_text(
        json.dumps({"episode_id": "ep_s", "approval_status": "MEDIA_READY"}), encoding="utf-8"
    )
    assert git_sync.sync_git_catalogue() is True
    assert json.loads(remote_file(repos.bare, "topics_history.json")) == [{"slug": "s"}]
    assert json.loads(remote_file(repos.bare, "tts_usage.json")) == {"chars": 5}
    assert json.loads(remote_file(repos.bare, "state.json"))["episodes"]["ep_s"]["approval_status"] == "MEDIA_READY"
    assert main_sha(repos.bare) == before
    assert git(repos.bare, "ls-tree", "--name-only", "main").split() == ["code.py", "config"]
    assert git(repos.work, "branch", "-r", "--list", "origin/main")


def test_hydrate_copies_remote_runtime_files_into_local_config(repos, tmp_path, monkeypatch):
    state_store.save_topics_history([{"slug": "remote"}])
    fresh = clone(repos.bare, tmp_path / "fresh")
    monkeypatch.setattr(state_store, "ROOT", fresh)
    assert state_store.hydrate_runtime_files() == ["topics_history.json"]
    assert json.loads((fresh / "config" / "topics_history.json").read_text(encoding="utf-8")) == [{"slug": "remote"}]
    assert (fresh / "config" / "archive_index.json").read_text(encoding="utf-8") == "[]\n"


def test_lost_push_race_is_retried_without_losing_either_write(repos, tmp_path, monkeypatch):
    state_store.ensure_automation_branch()
    rival = clone(repos.bare, tmp_path / "rival")
    original = state_store._push
    raced = []

    def racing_push(commit):
        if not raced:
            raced.append(True)
            monkeypatch.setattr(state_store, "ROOT", rival)
            assert state_store.save_episode_state("rival", {"n": 2})
            monkeypatch.setattr(state_store, "ROOT", repos.work)
        return original(commit)

    monkeypatch.setattr(state_store, "_push", racing_push)
    assert state_store.save_episode_state("mine", {"n": 1})
    episodes = json.loads(remote_file(repos.bare, "state.json"))["episodes"]
    assert episodes == {"mine": {"n": 1}, "rival": {"n": 2}}


@pytest.fixture
def review_env(repos, monkeypatch):
    sent = []
    monkeypatch.setattr(cloud_pipeline, "send_message", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(cloud_pipeline, "set_index_status", lambda *a, **k: None)
    monkeypatch.setattr(cloud_pipeline, "modify_remote", lambda *a, **k: None)
    monkeypatch.setattr(cloud_pipeline, "remote_status", lambda identifier: "REVIEW_PENDING")
    monkeypatch.setattr(cloud_pipeline, "archive_metadata", lambda identifier: {
        "metadata": {"title": "t", "approval_status": "REVIEW_PENDING"}})
    monkeypatch.setattr("archive_recovery.ensure_index_record", lambda *a, **k: True)
    return repos


def remote_episode(bare, key):
    return json.loads(remote_file(bare, "state.json"))["episodes"][key]


def test_review_publish_persists_state_to_automation_state(review_env):
    before = main_sha(review_env.bare)
    result = {"video_id": "yt1", "url": "https://www.youtube.com/watch?v=yt1"}
    with patch("youtube_publish.publish_archive_item", return_value=result):
        cloud_pipeline.publish("ia-item")
    episode = remote_episode(review_env.bare, "ia-item")
    assert episode["approval_status"] == "PUBLISHED"
    assert episode["youtube_video_id"] == "yt1"
    assert main_sha(review_env.bare) == before


def test_review_publish_owner_action_is_preserved_in_state(review_env):
    blocked = {"requires_owner_action": True, "status": "OWNER_ACTION_REQUIRED",
               "action": "REAUTHORIZE_YOUTUBE_OAUTH", "video_id": None}
    with patch("youtube_publish.publish_archive_item", return_value=blocked):
        assert cloud_pipeline.publish("ia-item")["requires_owner_action"]
    episode = remote_episode(review_env.bare, "ia-item")
    assert episode["approval_status"] == "REVIEW_PENDING"
    assert episode["publish_blocked"] == "OWNER_ACTION_REQUIRED"


def test_review_reject_persists_state_to_automation_state(review_env):
    with patch("cloud_pipeline.delete_rejected"):
        cloud_pipeline.reject("ia-item")
    assert remote_episode(review_env.bare, "ia-item")["approval_status"] == "REJECTED"


def test_review_state_attaches_to_existing_production_episode(review_env):
    state_store.save_episode_state("ep_9", {"archive_identifier": "ia-item", "story_path": "stories/x"})
    with patch("cloud_pipeline.delete_rejected"):
        cloud_pipeline.reject("ia-item")
    episodes = json.loads(remote_file(review_env.bare, "state.json"))["episodes"]
    assert list(episodes) == ["ep_9"]
    assert episodes["ep_9"]["approval_status"] == "REJECTED"
    assert episodes["ep_9"]["story_path"] == "stories/x"


def test_no_code_path_pushes_runtime_state_to_main():
    offenders = []
    for path in [*(REPO / "scripts").glob("*.py"), *(REPO / ".github" / "workflows").glob("*.yml")]:
        text = path.read_text(encoding="utf-8")
        if re.search(r"HEAD:main|push\W+origin\W+main|pull\W+--rebase\W+origin\W+main", text):
            offenders.append(path.name)
    assert offenders == []


@pytest.mark.parametrize("workflow", ["production.yml", "review.yml"])
def test_workflows_hydrate_and_persist_through_automation_state(workflow):
    text = (REPO / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    assert "scripts/state_store.py ensure" in text
    assert "scripts/state_store.py hydrate" in text
    assert "scripts/git_sync.py" in text
    assert "git push" not in text


def test_state_store_never_checks_out_or_switches_branches():
    source = (REPO / "scripts" / "state_store.py").read_text(encoding="utf-8")
    assert not re.search(r'"(checkout|switch|reset|clean|stash)"', source)
