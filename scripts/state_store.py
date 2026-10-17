"""Durable runtime state stored on the remote ``automation-state`` branch.

Code and configuration live on ``main``; runtime state (episode records,
archive index, topic history, TTS quota catalogues) lives only on
``origin/automation-state``. Every read and write goes through git plumbing
(fetch, ls-tree, cat-file, hash-object, mktree, commit-tree, push of a raw
commit), so the working tree, index and checked-out branch are never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import episode_id as episode_ids


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
    "pronunciation_dictionary.json",
)
PUSH_ATTEMPTS = 4
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


STATUS_RANK = {
    "GENERATING": 1, "FAILED_RETRYABLE": 1, "MEDIA_READY": 2, "REVIEW_PENDING": 3,
    "APPROVED_FOR_PUBLISH": 4, "PUBLISHING": 5, "PUBLISHED": 6, "REJECTED": 6,
}


def _story_name(record: dict) -> str:
    raw = str(record.get("story_path") or "").replace("\\", "/")
    return Path(raw).name if raw else ""


def _identities(key: str, record: dict) -> set[str]:
    identities = {f"k:{key}"}
    for field in ("archive_identifier", "identifier"):
        if record.get(field):
            identities.add(f"a:{record[field]}")
    if _story_name(record):
        identities.add(f"p:{_story_name(record)}")
    derived = record.get("episode_id") or episode_ids.extract_episode_id_from_path(record.get("story_path") or "")
    if derived:
        identities.add(f"e:{derived}")
    for legacy in record.get("legacy_keys") or []:
        identities.add(f"k:{legacy}")
    return identities


def _rank(record: dict) -> int:
    return STATUS_RANK.get(str(record.get("approval_status") or "").upper(), 0)


def _merge_records(records: list[dict]) -> dict:
    """Merge duplicate records of one episode without dropping useful fields."""
    merged: dict = {}
    for record in sorted(records, key=_rank):
        for field, value in record.items():
            if value in (None, "", [], {}) and field in merged:
                continue
            merged[field] = value
    return merged


def _canonical_for(group: dict[str, dict], context: list[dict]) -> str:
    for key, record in group.items():
        if episode_ids.is_episode_id(record.get("episode_id")):
            return record["episode_id"]
    for key in group:
        if episode_ids.is_episode_id(key):
            return key
    members = list(group.values())
    resolved = episode_ids.resolve_episode_id(
        records=[*members, *context],
        story_path=next((r.get("story_path") for r in members if r.get("story_path")), None),
        slug=next((r.get("slug") for r in members if r.get("slug")), None),
        archive_identifier=next((r.get("archive_identifier") for r in members if r.get("archive_identifier")), None),
    )
    if resolved:
        return resolved
    named = next((k for k in group if k != INCOMING), None)
    return named or (group[INCOMING].get("legacy_keys") or [INCOMING])[0]


def _normalize_episodes(episodes: dict, context: list[dict]) -> dict:
    """Collapse records that describe one episode into a single record keyed by episode_id."""
    parents = {key: key for key in episodes}

    def find(key: str) -> str:
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    owner: dict[str, str] = {}
    for key, record in episodes.items():
        for identity in _identities(key, record):
            if identity in owner:
                parents[find(key)] = find(owner[identity])
            else:
                owner[identity] = key
    groups: dict[str, dict[str, dict]] = {}
    for key, record in episodes.items():
        groups.setdefault(find(key), {})[key] = record
    result: dict = {}
    for group in groups.values():
        canonical = _canonical_for(group, context)
        merged = _merge_records(list(group.values()))
        if episode_ids.is_episode_id(canonical):
            merged["episode_id"] = canonical
        else:
            merged.pop("episode_id", None)
        legacy = sorted({k for k in group if k != canonical} | set(merged.get("legacy_keys") or []))
        if legacy:
            merged["legacy_keys"] = [k for k in legacy if k != canonical]
        if canonical in result:
            merged = _merge_records([result[canonical], merged])
            if episode_ids.is_episode_id(canonical):
                merged["episode_id"] = canonical
        result[canonical] = merged
    return result


def _context_records(read) -> list[dict]:
    """Archive index and topic history records used to resolve legacy identities."""
    records: list[dict] = []
    for name in (ARCHIVE_INDEX_FILE, TOPICS_HISTORY_FILE):
        content = read(name)
        if content is None:
            legacy = _legacy_path(name)
            content = legacy.read_text(encoding="utf-8") if legacy.exists() else None
        value = _loads(content, [])
        records += [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []
    return records


INCOMING = "\u0000incoming"


def _apply_episode_update(read, key_hint: str, archive_identifier: str | None, state: dict) -> dict:
    current = _loads(read(STATE_FILE), None)
    current = current if isinstance(current, dict) else _empty_state()
    return _apply_to(current, _context_records(read), key_hint, archive_identifier, state)


def _apply_to(current: dict, context: list[dict], key_hint: str, archive_identifier: str | None, state: dict) -> dict:
    incoming = dict(state)
    if archive_identifier:
        incoming["archive_identifier"] = archive_identifier
    if episode_ids.is_episode_id(key_hint):
        incoming["episode_id"] = key_hint
    elif key_hint or archive_identifier:
        incoming["legacy_keys"] = [key_hint or archive_identifier]
    if not incoming.get("episode_id"):
        resolved = episode_ids.resolve_episode_id(
            records=context,
            story_path=incoming.get("story_path"),
            slug=incoming.get("slug"),
            archive_identifier=incoming.get("archive_identifier"),
        )
        if resolved:
            incoming["episode_id"] = resolved
    pool = {**current.get("episodes", {}), INCOMING: incoming}
    normalized = _normalize_episodes(pool, context)
    target = next(key for key, record in normalized.items() if INCOMING in (record.get("legacy_keys") or []))
    record = normalized[target]
    record["legacy_keys"] = [k for k in record["legacy_keys"] if k != INCOMING]
    if not record["legacy_keys"]:
        del record["legacy_keys"]
    stored_rank = _rank(record)
    for field, value in incoming.items():
        if field == "legacy_keys":
            continue
        if field == "approval_status" and STATUS_RANK.get(str(value or "").upper(), 0) < stored_rank:
            continue
        record[field] = value
    current["episodes"] = normalized
    return current


def resolve_episode_for_archive(archive_identifier: str) -> str | None:
    """archive_identifier -> canonical episode_id using state, archive index and metadata."""
    state = _load_state()
    episodes = _normalize_episodes(state.get("episodes", {}), [])
    for key, record in episodes.items():
        if record.get("archive_identifier") == archive_identifier or archive_identifier in (record.get("legacy_keys") or []):
            return key
    for name in (ARCHIVE_INDEX_FILE, TOPICS_HISTORY_FILE):
        remote = _loads(_read_remote(name), None)
        records = remote if isinstance(remote, list) else _loads(
            _legacy_path(name).read_text(encoding="utf-8") if _legacy_path(name).exists() else "[]", [])
        for record in records:
            if archive_identifier in (record.get("identifier"), record.get("archive_identifier")) and record.get("episode_id"):
                return record["episode_id"]
    return None


def load_episode_state(episode_id: str) -> dict:
    try:
        episodes = _normalize_episodes(_load_state().get("episodes", {}), [])
        episode = episodes.get(episode_id) or next(
            (r for k, r in episodes.items()
             if episode_id in (r.get("legacy_keys") or []) or episode_id in (r.get("archive_identifier"), _story_name(r))),
            None,
        )
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


def _save_episode(key_hint: str, archive_identifier: str | None, state: dict, message: str) -> bool:
    def mutate(read):
        return {STATE_FILE: _dump(_apply_episode_update(read, key_hint, archive_identifier, state))}

    try:
        return _update_remote(mutate, message)
    except Exception as exc:
        print(f"Warning: could not save episode state for {key_hint or archive_identifier}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def save_episode_state(episode_id: str, state: dict) -> bool:
    """Merge ``state`` into the one canonical record of the episode on automation-state."""
    saved = _save_episode(episode_id, state.get("archive_identifier"), state, f"Save episode state for {episode_id}")
    if saved:
        return True
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
    """Same canonical record as save_episode_state, addressed by archive identifier."""
    return _save_episode(state.get("episode_id") or "", archive_identifier, state, f"Save episode state for {archive_identifier}")


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
        story_path = meta_file.parent.relative_to(root).as_posix()
        episode_id = (
            metadata.get("episode_id")
            or episode_ids.extract_episode_id_from_path(story_path)
            or meta_file.parent.name
        )
        states[episode_id] = {
            "approval_status": metadata.get("approval_status"),
            "youtube_video_id": metadata.get("youtube_video_id"),
            "story_path": story_path,
            "slug": metadata.get("slug"),
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

    def mutate(read):
        changes = dict(local_files)
        current = _loads(read(STATE_FILE), None)
        current = current if isinstance(current, dict) else _empty_state()
        context = _context_records(read)
        for episode_id, state in episodes.items():
            current = _apply_to(current, context, episode_id, state.get("archive_identifier"), {
                k: v for k, v in state.items() if v is not None})
        current["episodes"] = _normalize_episodes(current.get("episodes", {}), context)
        changes[STATE_FILE] = _dump(current)
        return changes

    return _update_remote(mutate, "Persist runtime state")


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
