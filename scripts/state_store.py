"""Durable runtime state stored on the remote ``automation-state`` branch.

Code and configuration live on ``main``; runtime state (episode records,
archive index, topic history, TTS quota catalogues) lives only on
``origin/automation-state``. Every read and write goes through git plumbing
(fetch, ls-tree, cat-file, hash-object, mktree, commit-tree, push of a raw
commit), so the working tree, index and checked-out branch are never touched.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_STATE_BRANCH = "automation-state"
REMOTE = "origin"
STATE_FILE = "state.json"
ARCHIVE_INDEX_FILE = "archive_index.json"
TOPICS_HISTORY_FILE = "topics_history.json"
RUNTIME_FILES = (
    TOPICS_HISTORY_FILE,
    ARCHIVE_INDEX_FILE,
    "topic_candidate_bank.json",
    "tts_usage.json",
    "tts_quota_state.json",
    "llm_quota_state.json",
    "pronunciation_dictionary.json",
)
PUSH_ATTEMPTS = 4
CHECKPOINT_DIR = "checkpoints"
CHECKPOINT_MAX_FILE_BYTES = 400_000
CHECKPOINT_MAX_TOTAL_BYTES = 3_000_000
CHECKPOINT_TEXT_SUFFIXES = (".json", ".md", ".txt")
CHECKPOINT_ROOT_FILES = ("03_sources.md", "04_research_notes.md", "01_title.txt", "02_description.txt")
IDENTITY = {
    "GIT_AUTHOR_NAME": "sleep-history-bot",
    "GIT_AUTHOR_EMAIL": "marwalfarsi2000@gmail.com",
    "GIT_COMMITTER_NAME": "sleep-history-bot",
    "GIT_COMMITTER_EMAIL": "marwalfarsi2000@gmail.com",
}


class StateStoreError(RuntimeError):
    pass


def _remote_ref() -> str:
    return f"refs/remotes/{REMOTE}/{AUTOMATION_STATE_BRANCH}"


def _git(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **IDENTITY},
    )
    if check and result.returncode != 0:
        raise StateStoreError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result


def _empty_state() -> dict:
    return {"version": 2, "episodes": {}}


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def remote_branch_exists() -> bool:
    result = _git("ls-remote", "--exit-code", "--heads", REMOTE, AUTOMATION_STATE_BRANCH, check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise StateStoreError(f"cannot query remote {REMOTE}: {result.stderr.strip()}")


def _tip() -> str | None:
    result = _git("rev-parse", "--verify", "-q", _remote_ref(), check=False)
    return result.stdout.strip() or None if result.returncode == 0 else None


def _fetch() -> str | None:
    """Update the remote-tracking ref without touching the working tree."""
    refspec = f"+refs/heads/{AUTOMATION_STATE_BRANCH}:{_remote_ref()}"
    result = _git("fetch", "--no-tags", REMOTE, refspec, check=False)
    if result.returncode != 0:
        print(f"Warning: could not fetch {REMOTE}/{AUTOMATION_STATE_BRANCH}: {result.stderr.strip()}", file=sys.stderr)
    return _tip()


def _tree_entries(tip: str | None) -> dict[str, str]:
    if not tip:
        return {}
    listing = _git("ls-tree", tip).stdout
    entries: dict[str, str] = {}
    for line in listing.splitlines():
        meta, name = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob":
            entries[name] = sha
    return entries


def _blob(sha: str) -> str:
    return _git("cat-file", "blob", sha).stdout


def _write_blob(content: str) -> str:
    return _git("hash-object", "-w", "--stdin", input=content).stdout.strip()


def _commit_files(files: dict[str, str], parent: str | None, message: str) -> tuple[str, str]:
    entries = _tree_entries(parent)
    for name, content in files.items():
        entries[name] = _write_blob(content)
    spec = "".join(f"100644 blob {sha}\t{name}\n" for name, sha in sorted(entries.items()))
    tree = _git("mktree", input=spec).stdout.strip()
    args = ["commit-tree", tree, "-m", message]
    if parent:
        args[2:2] = ["-p", parent]
    commit = _git(*args).stdout.strip()
    return tree, commit


def _push(commit: str) -> bool:
    result = _git("push", REMOTE, f"{commit}:refs/heads/{AUTOMATION_STATE_BRANCH}", check=False)
    return result.returncode == 0


def ensure_automation_branch() -> bool:
    """Create the remote automation-state branch (orphan root commit) if absent."""
    try:
        for attempt in range(PUSH_ATTEMPTS):
            if remote_branch_exists():
                _fetch()
                return True
            _, commit = _commit_files({STATE_FILE: _dump(_empty_state())}, None, "Initialize automation-state")
            if _push(commit):
                _fetch()
                return True
            time.sleep(2 ** attempt)
        return remote_branch_exists()
    except Exception as exc:
        print(f"Warning: could not ensure {AUTOMATION_STATE_BRANCH}: {exc}", file=sys.stderr)
        return False


_ensure_automation_branch = ensure_automation_branch


def _read_files(names: tuple[str, ...] | list[str]) -> dict[str, str | None]:
    tip = _fetch()
    entries = _tree_entries(tip)
    return {name: (_blob(entries[name]) if name in entries else None) for name in names}


def _read_remote(name: str) -> str | None:
    return _read_files([name])[name]


def _update_remote(mutator: Callable[[Callable[[str], str | None]], dict[str, str]], message: str) -> bool:
    """Apply ``mutator`` to the latest remote state and push a fast-forward commit.

    ``mutator`` receives a ``read(name)`` callable and returns ``{name: content}``
    for the files to change. On a lost race the whole read-modify-write repeats.
    """
    for attempt in range(PUSH_ATTEMPTS):
        tip = _fetch()
        if tip is None:
            if not ensure_automation_branch():
                raise StateStoreError("automation-state branch is unavailable")
            tip = _tip()
        entries = _tree_entries(tip)

        def read(name: str) -> str | None:
            return _blob(entries[name]) if name in entries else None

        changes = {
            name: content for name, content in mutator(read).items()
            if read(name) != content
        }
        if not changes:
            return True
        _, commit = _commit_files(changes, tip, message)
        if _push(commit):
            _git("update-ref", _remote_ref(), commit)
            return True
        time.sleep(2 ** attempt)
    raise StateStoreError(f"could not push {AUTOMATION_STATE_BRANCH} after {PUSH_ATTEMPTS} attempts")


def _loads(text: str | None, default: object) -> object:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _legacy_path(name: str) -> Path:
    return ROOT / "config" / name


def _mirror_local(name: str, value: object) -> None:
    try:
        path = _legacy_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(value), encoding="utf-8")
    except OSError:
        pass


def _load_state() -> dict:
    state = _loads(_read_remote(STATE_FILE), None)
    return state if isinstance(state, dict) else _empty_state()


def load_episode_state(episode_id: str) -> dict:
    try:
        episode = _load_state().get("episodes", {}).get(episode_id)
        if episode:
            return episode
    except Exception as exc:
        print(f"Warning: remote episode state unavailable: {exc}", file=sys.stderr)
    legacy = _legacy_path(TOPICS_HISTORY_FILE)
    if legacy.exists():
        for topic in json.loads(legacy.read_text(encoding="utf-8")):
            if topic.get("story_path") and episode_id in topic["story_path"]:
                return topic
    return {}


def save_episode_state(episode_id: str, state: dict) -> bool:
    """Merge ``state`` into the episode record on automation-state."""
    def mutate(read):
        current = _loads(read(STATE_FILE), None)
        current = current if isinstance(current, dict) else _empty_state()
        episodes = current.setdefault("episodes", {})
        episodes[episode_id] = {**episodes.get(episode_id, {}), **state}
        return {STATE_FILE: _dump(current)}

    try:
        return _update_remote(mutate, f"Save episode state for {episode_id}")
    except Exception as exc:
        print(f"Warning: could not save episode state for {episode_id}: {exc}", file=sys.stderr)
    legacy = _legacy_path(TOPICS_HISTORY_FILE)
    if legacy.exists():
        try:
            topics = json.loads(legacy.read_text(encoding="utf-8"))
            for topic in topics:
                if topic.get("story_path") and episode_id in topic["story_path"]:
                    topic.update(state)
                    legacy.write_text(_dump(topics), encoding="utf-8")
                    break
        except (OSError, json.JSONDecodeError):
            pass
    return False


def save_episode_for_archive(archive_identifier: str, state: dict) -> bool:
    """Merge ``state`` into the episode whose archive_identifier matches.

    Falls back to using the archive identifier as the episode key.
    """
    def mutate(read):
        current = _loads(read(STATE_FILE), None)
        current = current if isinstance(current, dict) else _empty_state()
        episodes = current.setdefault("episodes", {})
        key = next(
            (k for k, v in episodes.items() if v.get("archive_identifier") == archive_identifier),
            archive_identifier,
        )
        episodes[key] = {**episodes.get(key, {}), "archive_identifier": archive_identifier, **state}
        return {STATE_FILE: _dump(current)}

    try:
        return _update_remote(mutate, f"Save episode state for {archive_identifier}")
    except Exception as exc:
        print(f"Warning: could not save episode state for {archive_identifier}: {exc}", file=sys.stderr)
        return False


def _load_list(name: str) -> list:
    try:
        remote = _loads(_read_remote(name), None)
        if isinstance(remote, list):
            return remote
    except Exception as exc:
        print(f"Warning: remote {name} unavailable: {exc}", file=sys.stderr)
    legacy = _legacy_path(name)
    return json.loads(legacy.read_text(encoding="utf-8")) if legacy.exists() else []


def _save_list(name: str, value: list) -> bool:
    _mirror_local(name, value)
    try:
        return _update_remote(lambda read: {name: _dump(value)}, f"Update {name}")
    except Exception as exc:
        print(f"Warning: could not save {name} to {AUTOMATION_STATE_BRANCH}: {exc}", file=sys.stderr)
        return False


def load_archive_index() -> list[dict]:
    return _load_list(ARCHIVE_INDEX_FILE)


def save_archive_index(index: list[dict]) -> bool:
    return _save_list(ARCHIVE_INDEX_FILE, index)


def load_topics_history() -> list[dict]:
    return _load_list(TOPICS_HISTORY_FILE)


def save_topics_history(history: list[dict]) -> bool:
    return _save_list(TOPICS_HISTORY_FILE, history)


def hydrate_runtime_files() -> list[str]:
    """Copy runtime catalogues from automation-state into the local config dir.

    Files absent on the branch keep their legacy checked-out copy.
    """
    remote = _read_files(RUNTIME_FILES)
    hydrated = []
    for name, content in remote.items():
        if content is None:
            continue
        path = _legacy_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        hydrated.append(name)
    return hydrated


def ensure_hydrated() -> bool:
    """Hydrate runtime catalogues once per runner; safe to call from every entry point."""
    marker = ROOT / ".runtime" / "hydrated.json"
    if marker.exists():
        return True
    try:
        hydrated = hydrate_runtime_files()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"hydrated": hydrated}), encoding="utf-8")
        return True
    except Exception as exc:
        print(f"Warning: runtime state hydration skipped: {exc}", file=sys.stderr)
        return False


def _story_episode_states(root: Path) -> dict[str, dict]:
    states: dict[str, dict] = {}
    for meta_file in sorted(root.glob("stories/*/15_metadata.json")):
        try:
            metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        episode_id = metadata.get("episode_id") or meta_file.parent.name
        states[episode_id] = {
            "approval_status": metadata.get("approval_status"),
            "youtube_video_id": metadata.get("youtube_video_id"),
            "story_path": meta_file.parent.relative_to(root).as_posix(),
        }
    return states


def persist_all() -> bool:
    """Push local runtime catalogues and story episode records to automation-state."""
    local_files = {}
    for name in RUNTIME_FILES:
        path = _legacy_path(name)
        if path.exists():
            local_files[name] = path.read_text(encoding="utf-8")
    episodes = _story_episode_states(ROOT)
    for story in sorted(ROOT.glob("stories/*")):
        metadata = story / "15_metadata.json"
        status = ""
        if metadata.exists():
            try:
                status = str(json.loads(metadata.read_text(encoding="utf-8")).get("approval_status") or "")
            except (OSError, json.JSONDecodeError):
                status = ""
        if status in ("", "GENERATING", "generating", "drafting"):  # unfinished: keep a durable copy
            bundle = collect_story_checkpoint(story)
            if bundle:
                local_files[checkpoint_file(story.name)] = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))

    def mutate(read):
        changes = dict(local_files)
        if episodes:
            current = _loads(read(STATE_FILE), None)
            current = current if isinstance(current, dict) else _empty_state()
            records = current.setdefault("episodes", {})
            for episode_id, state in episodes.items():
                records[episode_id] = {**records.get(episode_id, {}), **state}
            changes[STATE_FILE] = _dump(current)
        return changes

    return _update_remote(mutate, "Persist runtime state")


def checkpoint_file(story_name: str) -> str:
    return f"{CHECKPOINT_DIR}/{story_name}.json"


def collect_story_checkpoint(story: Path) -> dict | None:
    """Small text state needed to rebuild an unfinished episode: topic, research, sources, outline, chapters.

    Audio, images, video and logs are never included; those live in artifacts or Internet Archive.
    """
    logs = story / "logs"
    if not (logs / "topic.json").exists():
        return None
    files: dict[str, str] = {}
    total = 0
    candidates = [path for path in sorted(logs.glob("*")) if path.suffix in CHECKPOINT_TEXT_SUFFIXES]
    candidates += [story / name for name in CHECKPOINT_ROOT_FILES if (story / name).exists()]
    for path in candidates:
        try:
            size = path.stat().st_size
            if not path.is_file() or size > CHECKPOINT_MAX_FILE_BYTES or total + size > CHECKPOINT_MAX_TOTAL_BYTES:
                continue
            files[path.relative_to(story).as_posix()] = path.read_text(encoding="utf-8")
            total += size
        except (OSError, UnicodeDecodeError):
            continue
    return {"version": 1, "story": story.name, "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(), "files": files}


def save_story_checkpoint(story: Path) -> bool:
    """Persist the small checkpoint bundle to automation-state (never to main)."""
    bundle = collect_story_checkpoint(story)
    if not bundle:
        return False
    name = checkpoint_file(story.name)
    return _update_remote(lambda read: {name: json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))},
                          f"Checkpoint {story.name}")


def _safe_checkpoint_path(relative: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    if ".." in parts or relative.startswith("/") or not parts[-1].endswith(CHECKPOINT_TEXT_SUFFIXES):
        return False
    return (len(parts) == 2 and parts[0] == "logs") or (len(parts) == 1 and relative in CHECKPOINT_ROOT_FILES)


def restore_story_checkpoint(root: Path, story_name: str) -> int:
    """Rebuild missing text files of an unfinished story from automation-state; returns files restored."""
    raw = _read_remote(checkpoint_file(story_name))
    bundle = _loads(raw, None)
    if not isinstance(bundle, dict) or not isinstance(bundle.get("files"), dict):
        return 0
    restored = 0
    story = root / "stories" / story_name
    for relative, content in bundle["files"].items():
        if not isinstance(content, str) or not _safe_checkpoint_path(relative):
            continue
        destination = story / relative
        if destination.exists():
            continue  # never overwrite newer local work
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        restored += 1
    return restored


def compute_rules_sha(rules_path: Path) -> str:
    if not rules_path.exists():
        return ""
    return hashlib.sha256(rules_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    command = (argv if argv is not None else sys.argv[1:] or [""])[0]
    if command == "ensure":
        return 0 if ensure_automation_branch() else 1
    if command == "hydrate":
        print(json.dumps({"hydrated": hydrate_runtime_files()}))
        return 0
    if command == "persist":
        return 0 if persist_all() else 1
    print("usage: state_store.py ensure|hydrate|persist", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
