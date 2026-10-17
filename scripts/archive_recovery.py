"""Self-healing Internet Archive catalogue recovery.

Recovers missing episode state from Internet Archive metadata when local
state is incomplete. This supports resuming interrupted episodes without
manual intervention.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def remote_url(identifier: str, filename: str) -> str:
    """Construct Internet Archive download URL."""
    return "https://archive.org/download/{}/{}".format(
        urllib.parse.quote(identifier, safe=""), urllib.parse.quote(filename)
    )


def download_metadata(identifier: str) -> dict:
    """Download 15_metadata.json from Internet Archive item."""
    try:
        with urllib.request.urlopen(
            remote_url(identifier, "15_metadata.json"), timeout=60
        ) as response:
            text = response.read().decode("utf-8-sig").strip()
            return json.loads(text)
    except Exception as exc:
        print(f"Warning: Could not fetch metadata from {identifier}: {exc}")
        return {}


def ensure_index_record(identifier: str, access_key: str = "", secret_key: str = "") -> bool:
    """Ensure episode exists in local index.

    If record is missing:
    1. Fetch Internet Archive metadata
    2. Confirm item exists
    3. Reconstruct runtime record from archived metadata
    4. Update local state
    """
    from archive_storage import get_archive_item, set_index_status

    archive_index_path = ROOT / "config" / "archive_index.json"
    if not archive_index_path.exists():
        archive_index_path.write_text("[]", encoding="utf-8")

    archive_index = json.loads(archive_index_path.read_text(encoding="utf-8"))

    for record in archive_index:
        if record.get("identifier") == identifier:
            return True

    try:
        if access_key and secret_key:
            item = get_archive_item(identifier, access_key=access_key, secret_key=secret_key)
            if not item or not item.exists:
                print(f"Warning: Item {identifier} not found in Internet Archive")
                return False

        metadata = download_metadata(identifier)
        if not metadata:
            print(f"Warning: No metadata found for {identifier}")
            return False

        record = {
            "identifier": identifier,
            "episode_id": metadata.get("episode_id", ""),
            "slug": metadata.get("slug", ""),
            "title": metadata.get("title", metadata.get("final_youtube_title", "")),
            "episode_title_core": metadata.get("episode_title_core", ""),
            "title_suffix": metadata.get("title_suffix", ""),
            "final_youtube_title": metadata.get("final_youtube_title", ""),
            "thumbnail_text": metadata.get("thumbnail_text", ""),
            "thumbnail_prompt": metadata.get("thumbnail_prompt", ""),
            "historical_setting": metadata.get("historical_setting", ""),
            "created_at": metadata.get("created_at", ""),
            "approval_status": metadata.get("approval_status", "REVIEW_PENDING"),
            "youtube_video_id": metadata.get("youtube_video_id", ""),
            "archive_url": f"https://archive.org/details/{identifier}",
        }

        archive_index.append(record)
        archive_index_path.write_text(
            json.dumps(archive_index, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        topics_history_path = ROOT / "config" / "topics_history.json"
        if topics_history_path.exists():
            topics = json.loads(topics_history_path.read_text(encoding="utf-8"))
            for topic in topics:
                if (
                    topic.get("archive_identifier") == identifier
                    or topic.get("slug") == metadata.get("slug")
                ):
                    topic.update({
                        "archive_identifier": identifier,
                        "archive_url": f"https://archive.org/details/{identifier}",
                        "status": metadata.get("approval_status", topic.get("status", "REVIEW_PENDING")),
                        "youtube_video_id": metadata.get("youtube_video_id") or topic.get("youtube_video_id"),
                    })
                    topics_history_path.write_text(
                        json.dumps(topics, ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )
                    break

        print(f"Recovered record for {identifier} from Internet Archive metadata")
        return True
    except Exception as exc:
        print(f"Error recovering record for {identifier}: {exc}")
        return False


def verify_item_exists(identifier: str, access_key: str = "", secret_key: str = "") -> bool:
    """Verify that an Internet Archive item exists."""
    try:
        from archive_storage import get_archive_item
        item = get_archive_item(identifier, access_key=access_key, secret_key=secret_key)
        return bool(item) and item.exists
    except Exception:
        return False
