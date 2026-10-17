"""Stable episode identifier generation and management."""

from __future__ import annotations

import hashlib
import datetime as dt
from pathlib import Path


def generate_episode_id(
    date: dt.date,
    slug: str,
    title: str,
) -> str:
    """Generate stable episode ID.

    Format: ep_YYYYMMDD_<short-slug>_<short-hash>

    The ID is stable across retries and checkpoint restores because it only
    depends on immutable topic metadata, not final YouTube title or other
    derived data.
    """
    date_part = date.strftime("%Y%m%d")
    slug_part = slug.replace("-", "_")[:12]
    content = f"{date}|{slug}|{title}".encode("utf-8")
    hash_part = hashlib.sha256(content).hexdigest()[:8]
    return f"ep_{date_part}_{slug_part}_{hash_part}"


def extract_episode_id_from_path(story_path: Path) -> str | None:
    """Extract episode ID from story directory path.

    Expects format: stories/YYYY-MM-DD_<slug>
    """
    name = story_path.name
    if "_" not in name:
        return None
    date_part, slug_part = name.split("_", 1)
    try:
        date = dt.date.fromisoformat(date_part)
        if not 3 <= len(slug_part) <= 50:
            return None
        return generate_episode_id(date, slug_part, slug_part)
    except (ValueError, TypeError):
        return None


def ensure_metadata_episode_id(metadata: dict, story_path: Path) -> str:
    """Ensure episode_id exists in metadata, generating if needed."""
    existing = metadata.get("episode_id", "").strip()
    if existing:
        return existing
    episode_id = extract_episode_id_from_path(story_path)
    if not episode_id:
        raise RuntimeError(f"Cannot generate episode_id from story path: {story_path}")
    metadata["episode_id"] = episode_id
    return episode_id
