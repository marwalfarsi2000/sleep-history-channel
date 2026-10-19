"""Regressions found by the first live run of the reliability PR (Production run 66)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import cloud_pipeline
import pipeline
import run_summary
import state_store

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


@pytest.fixture
def real_state_branch(tmp_path, monkeypatch):
    """A real bare remote + working repo: the branch tree is written with real git plumbing."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    work.mkdir()
    git(work, "init", "-q")
    git(work, "remote", "add", "origin", str(remote))
    monkeypatch.setattr(state_store, "ROOT", work)
    monkeypatch.setattr(state_store, "REMOTE", "origin")
    (work / "config").mkdir()
    assert state_store.ensure_automation_branch()
    return work


def make_story(root: Path, status: str | None = None) -> Path:
    story = root / "stories" / "2026-10-18_tripoli-lybia-karamanli-daily-life"
    (story / "logs").mkdir(parents=True)
    (story / "logs" / "topic.json").write_text('{"slug": "tripoli"}', encoding="utf-8")
    (story / "logs" / "outline.json").write_text('{"sections": []}', encoding="utf-8")
    if status:
        (story / "15_metadata.json").write_text(json.dumps({"approval_status": status}), encoding="utf-8")
    return story


def test_checkpoint_is_saved_and_restored_through_a_real_git_branch(real_state_branch, tmp_path):
    story = make_story(real_state_branch)
    assert state_store.save_story_checkpoint(story) is True          # failed with "contains slash" in run 66
    other = tmp_path / "fresh"
    assert state_store.restore_story_checkpoint(other, story.name) == 2
    assert (other / "stories" / story.name / "logs" / "outline.json").exists()


def test_end_of_run_persistence_stores_the_catalogue_and_the_checkpoint(real_state_branch):
    story = make_story(real_state_branch)
    (real_state_branch / "config" / "topics_history.json").write_text('[{"slug": "tripoli"}]', encoding="utf-8")
    assert state_store.persist_all() is True
    assert state_store._read_remote("topics_history.json") == '[{"slug": "tripoli"}]'
    assert json.loads(state_store._read_remote(state_store.checkpoint_file(story.name)))["story"] == story.name


def test_a_broken_checkpoint_never_blocks_the_catalogue_update(real_state_branch):
    make_story(real_state_branch)
    (real_state_branch / "config" / "topics_history.json").write_text('[{"slug": "kept"}]', encoding="utf-8")
    real_update = state_store._update_remote

    def fail_on_checkpoints(mutator, message):
        if "checkpoint" in message.lower():
            raise state_store.StateStoreError("boom")
        return real_update(mutator, message)
    with mock.patch.object(state_store, "_update_remote", fail_on_checkpoints):
        assert state_store.persist_all() is True
    assert state_store._read_remote("topics_history.json") == '[{"slug": "kept"}]'


def test_a_finished_stories_checkpoint_is_retired(real_state_branch):
    story = make_story(real_state_branch)
    state_store.persist_all()
    assert state_store._read_remote(state_store.checkpoint_file(story.name))
    (story / "15_metadata.json").write_text(json.dumps({"approval_status": "REVIEW_PENDING"}), encoding="utf-8")
    state_store.persist_all()
    assert state_store._read_remote(state_store.checkpoint_file(story.name)) is None


def test_branch_file_names_are_flat():
    assert "/" not in state_store.checkpoint_file("2026-10-18_x") and state_store.checkpoint_file("a").endswith(".json")


# ---- TTS: a run-on sentence is repaired locally, not fatal ------------------------------------------------

def test_a_runon_is_split_at_clause_punctuation_and_recorded():
    clause = "ثم يسير الناس في الأزقة الهادئة بين البيوت البيضاء"
    runon = "، ".join([clause] * 6) + "."           # no sentence-ending mark until the very end
    chunks = pipeline.split_chunks(runon + " جملة أخيرة قصيرة.", limit=200)
    assert all(len(chunk) <= 200 for chunk in chunks) and len(chunks) >= 2
    assert pipeline.LAST_CHUNK_REPAIRS and pipeline.LAST_CHUNK_REPAIRS[0]["kind"] == "clause"
    assert "".join(chunks).replace("\n", "").replace(" ", "").count("ثميسير") == 6      # nothing lost
    assert chunks[-1].endswith("قصيرة.")


def test_punctuation_free_text_is_split_by_words_only_within_a_small_budget():
    words = " ".join(["كلمة"] * 120)                  # 599 characters, no punctuation at all
    chunks = pipeline.split_chunks(words, limit=200)
    assert all(len(chunk) <= 200 for chunk in chunks) and pipeline.LAST_CHUNK_REPAIRS[0]["kind"] == "word"
    many = "\n\n".join(" ".join(["كلمة"] * 120) + "." for _ in range(pipeline.MAX_WORD_LEVEL_SPLITS + 1))
    with pytest.raises(RuntimeError, match="TTS_CHUNKING_FAILED.*without any punctuation"):
        pipeline.split_chunks(many, limit=200)


def test_systemic_runons_still_fail_closed():
    clause = "جملة قصيرة هادئة " * 4
    text = "\n\n".join("، ".join([clause] * 5) + "." for _ in range(pipeline.MAX_OVERLONG_SENTENCES + 1))
    with pytest.raises(RuntimeError, match="TTS_CHUNKING_FAILED"):
        pipeline.split_chunks(text, limit=200)


def test_normal_text_chunks_are_unchanged_and_record_no_repairs():
    text = "\n\n".join(f"الجملة الأولى هنا {i}. الجملة الثانية هنا؟" for i in range(40))
    chunks = pipeline.split_chunks(text, limit=300)
    assert pipeline.LAST_CHUNK_REPAIRS == [] and all(len(chunk) <= 300 for chunk in chunks)


# ---- zombie records persist: hydration must not undo the migration ----------------------------------------

def test_the_produce_command_retires_zombies_after_hydration(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    history = tmp_path / "config" / "topics_history.json"
    history.write_text(json.dumps([{"slug": "dhofar-incense-network", "status": "pending_approval",
                                    "story_path": "stories/2026-10-07_dhofar-incense-network"}]), encoding="utf-8")
    monkeypatch.setattr(cloud_pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(cloud_pipeline.sys, "argv", ["cloud_pipeline.py", "produce"])
    with mock.patch.object(cloud_pipeline, "dispatch_command", return_value={"ok": True}), \
            mock.patch.object(cloud_pipeline.state_store, "ensure_hydrated"):
        cloud_pipeline.main()
    assert json.loads(history.read_text(encoding="utf-8"))[0]["status"] == "ABANDONED_LEGACY"
