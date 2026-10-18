"""One canonical answer to "which episode is unfinished?" for restore and generation.

Priority: (1) the active episode recorded in automation-state, (2) the latest valid
unfinished topic-history record, (3) legacy statuses. Zombie or stale entries are
dropped BEFORE any checkpoint artifact is searched, so an old abandoned record can
never shadow the real active episode.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path, PurePosixPath

from state_machine import ACTIVE_STATES, State, normalize_state

ZOMBIE_AFTER_DAYS = 30  # matches failure-artifact retention; a durable checkpoint covers the rest
UPLOAD_INCOMPLETE = {State.REVIEW_PENDING, State.MEDIA_READY}
LEGACY_PENDING = {"pending_approval"}


def story_dir(record: dict, root: Path) -> Path | None:
    raw = str(record.get("story_path") or "").replace("\\", "/")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else root / path


def is_valid_checkpoint(record: dict, root: Path, now: dt.datetime | None = None, *, quiet: bool = False) -> bool:
    """False for zombie records: no folder and no usable created_at, or stale for over a week."""
    path = story_dir(record, root)
    if not path:
        return False
    log = (lambda *_: None) if quiet else (lambda message: print(message, file=sys.stderr))
    if not path.resolve().exists():
        created_at = record.get("created_at")
        if not created_at:
            log(f"Skipping zombie checkpoint missing created_at without local folder: {record.get('slug')}")
            return False
        try:
            created = dt.datetime.fromisoformat(str(created_at))
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt.timezone.utc)
            if ((now or dt.datetime.now(dt.timezone.utc)) - created.astimezone(dt.timezone.utc)).days > ZOMBIE_AFTER_DAYS:
                log(f"Skipping stale/zombie checkpoint without local folder: {record.get('slug')}")
                return False
        except ValueError:
            log(f"Skipping zombie checkpoint with malformed created_at without local folder: {record.get('slug')}")
            return False
        return True
    resolved = path.resolve()
    if not ((resolved / "logs" / "topic.json").exists() or (resolved / "15_metadata.json").exists()):
        if record.get("topic") or record.get("topic_ar") or record.get("slug"):
            return True
        log(f"Skipping corrupt checkpoint lacking topic/metadata: {record.get('slug')}")
        return False
    return True


def is_unfinished(record: dict) -> bool:
    status = normalize_state(str(record.get("status") or ""))
    raw = str(record.get("status") or "")
    has_archive = bool(str(record.get("archive_identifier") or "").strip())
    if status in ACTIVE_STATES:
        return True
    if (status in UPLOAD_INCOMPLETE or raw in LEGACY_PENDING) and not has_archive:
        return True
    return False


def active_episode(history: list[dict], root: Path, state_episodes: dict | None = None,
                   now: dt.datetime | None = None) -> dict | None:
    """The single unfinished episode to resume, or None."""
    valid = [r for r in history if is_unfinished(r) and is_valid_checkpoint(r, root, now)]
    if not valid:
        return None
    # 1. automation-state names an active episode: prefer the history record it points at.
    for episode in (state_episodes or {}).values():
        status = normalize_state(str(episode.get("approval_status") or ""))
        wanted = PurePosixPath(str(episode.get("story_path") or "").replace("\\", "/")).name
        if status in ACTIVE_STATES and wanted:
            for record in reversed(valid):
                if PurePosixPath(str(record.get("story_path") or "").replace("\\", "/")).name == wanted:
                    return record
    # 2. latest valid unfinished record (history is append-ordered).
    return valid[-1]


def migrate_zombie_records(history: list[dict], root: Path, now: dt.datetime | None = None) -> list[str]:
    """Retire unfinished records that can never be resumed, once, with an audit trail.

    A zombie has no story folder and no usable ``created_at`` (or is older than the retention
    window). It becomes ``ABANDONED_LEGACY`` so it stops producing a warning on every run. History
    is never deleted, and records with a folder, a recent ``created_at`` or an archive item are
    untouched. Idempotent: an already migrated record is skipped. Returns the migrated slugs.
    """
    migrated: list[str] = []
    stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    for record in history:
        if not is_unfinished(record) or is_valid_checkpoint(record, root, now, quiet=True):
            continue
        record["status_before_migration"] = record.get("status")
        record["status"] = State.ABANDONED_LEGACY
        record["migrated_at"] = stamp
        record["migration_reason"] = "zombie checkpoint: no story folder and no usable created_at / past retention"
        migrated.append(str(record.get("slug") or ""))
    return migrated
