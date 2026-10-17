"""Canonical, stable episode identity.

``episode_id`` is assigned once when a topic is selected and never changes. It is
derived only from immutable facts (story folder date + slug), so it is identical
across retries, resumes, regenerations, runners and status changes, and it is
independent of the Internet Archive identifier and of the story folder name used
by a particular runner. Archive identifiers and folder names are only legacy
lookup keys that resolve to the episode_id.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Iterable

EPISODE_ID_RE = re.compile(r"^ep_\d{8}_[a-z0-9_]{0,12}_[0-9a-f]{8}$")
STORY_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)$")


def is_episode_id(value: object) -> bool:
    return isinstance(value, str) and bool(EPISODE_ID_RE.match(value))


def generate_episode_id(date: dt.date, slug: str, title: str) -> str:
    """Format: ep_YYYYMMDD_<short-slug>_<short-hash>.

    Callers pass the immutable slug as ``title`` for the canonical id; a final
    YouTube title must never be used because it can change.
    """
    date_part = date.strftime("%Y%m%d")
    slug_part = re.sub(r"[^a-z0-9_]", "", slug.lower().replace("-", "_"))[:12]
    content = f"{date}|{slug}|{title}".encode("utf-8")
    return f"ep_{date_part}_{slug_part}_{hashlib.sha256(content).hexdigest()[:8]}"


def canonical_episode_id(date: dt.date, slug: str) -> str:
    return generate_episode_id(date, slug, slug)


def parse_story_name(name: str) -> tuple[dt.date, str] | None:
    match = STORY_NAME_RE.match(Path(str(name).replace("\\", "/")).name)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1)), match.group(2)
    except ValueError:
        return None


def extract_episode_id_from_path(story_path: Path | str) -> str | None:
    parsed = parse_story_name(str(story_path))
    if not parsed or not 3 <= len(parsed[1]) <= 80:
        return None
    return canonical_episode_id(*parsed)


def _matches(record: dict, slug: str | None, story_path: str | None, archive_identifier: str | None) -> bool:
    if archive_identifier and archive_identifier in (record.get("archive_identifier"), record.get("identifier")):
        return True
    record_path = str(record.get("story_path") or "").replace("\\", "/")
    if story_path and record_path and Path(record_path).name == Path(story_path).name:
        return True
    return bool(slug and record.get("slug") == slug)


def resolve_episode_id(
    *,
    metadata: dict | None = None,
    records: Iterable[dict] = (),
    story_path: Path | str | None = None,
    slug: str | None = None,
    archive_identifier: str | None = None,
    created_at: str | None = None,
) -> str | None:
    """Return the canonical id for an episode, deriving a deterministic one for legacy data.

    Precedence: metadata.episode_id, then any matching record's episode_id, then a
    deterministic derivation from the story folder date and slug.
    """
    metadata = metadata or {}
    if is_episode_id(metadata.get("episode_id")):
        return metadata["episode_id"]
    slug = slug or metadata.get("slug")
    path = str(story_path) if story_path else None
    matching = [r for r in records if _matches(r, slug, path, archive_identifier)]
    for record in matching:
        if is_episode_id(record.get("episode_id")):
            return record["episode_id"]
    for candidate in [path, *[str(r.get("story_path") or "") for r in matching]]:
        derived = extract_episode_id_from_path(candidate) if candidate else None
        if derived:
            return derived
    created = created_at or metadata.get("created_at") or next((r.get("created_at") for r in matching if r.get("created_at")), None)
    if slug and created:
        try:
            return canonical_episode_id(dt.datetime.fromisoformat(str(created)).date(), slug)
        except ValueError:
            return None
    return None


def ensure_metadata_episode_id(metadata: dict, story_path: Path, records: Iterable[dict] = ()) -> str:
    """Make sure metadata carries the canonical id (never replacing an existing one)."""
    existing = str(metadata.get("episode_id") or "").strip()
    if is_episode_id(existing):
        return existing
    resolved = resolve_episode_id(metadata=metadata, records=records, story_path=story_path, slug=metadata.get("slug"))
    if not resolved:
        raise RuntimeError(f"Cannot determine episode_id for story path: {story_path}")
    metadata["episode_id"] = resolved
    return resolved
