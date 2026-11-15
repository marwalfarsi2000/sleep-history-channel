from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from archive_storage import archive_metadata


ROOT = Path(__file__).resolve().parents[1]
SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_MANAGE = "https://www.googleapis.com/auth/youtube"
SCOPE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
MODE_DIRECT_PUBLIC = "approved_direct_public"
MODE_PRIVATE_VERIFY_PUBLIC = "private_verify_public"
DEFAULT_PUBLISH_MODE = MODE_PRIVATE_VERIFY_PUBLIC
INSERT_SCOPES = {SCOPE_UPLOAD, SCOPE_MANAGE, SCOPE_FORCE_SSL}
MANAGE_SCOPES = {SCOPE_MANAGE, SCOPE_FORCE_SSL}
READ_SCOPES = {SCOPE_READONLY, SCOPE_MANAGE, SCOPE_FORCE_SSL}
# Legacy code labelled every upload 4xx 'rejected', even after prior chunks.
# Historical records with that status therefore also require reconciliation.
UNCERTAIN_UPLOAD_STATES = {"inserting", "processing", "verified", "rejected"}
# Each requirement is (capability, any-of scopes); every requirement must be met.
MODE_REQUIREMENTS = {
    MODE_DIRECT_PUBLIC: [("videos.insert", INSERT_SCOPES)],
    MODE_PRIVATE_VERIFY_PUBLIC: [
        ("videos.insert", INSERT_SCOPES),
        ("videos.list and videos.update", MANAGE_SCOPES),
    ],
}


def load_settings() -> dict:
    return json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))


def publish_mode() -> str:
    mode = str(load_settings().get("youtube_publish_mode") or DEFAULT_PUBLISH_MODE)
    if mode not in MODE_REQUIREMENTS:
        raise RuntimeError(f"Unsupported youtube_publish_mode: {mode!r}")
    return mode


def get_granted_scopes(creds: Credentials) -> set[str]:
    """Refresh the token and return the scopes the refresh token really grants."""
    from google.auth.transport.requests import Request

    creds.refresh(Request())
    scopes = set(getattr(creds, "granted_scopes", None) or [])
    if not scopes and creds.token:
        url = "https://oauth2.googleapis.com/tokeninfo?access_token=" + urllib.parse.quote(creds.token, safe="")
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                info = json.loads(response.read().decode("utf-8"))
        except Exception:
            # HTTPError/URLError may contain the URL, including the access token.
            raise RuntimeError("YOUTUBE_OAUTH_PREFLIGHT_FAILED: token scope lookup failed") from None
        audience = info.get("aud") or info.get("issued_to")
        if audience and audience != creds.client_id:
            raise RuntimeError("YOUTUBE_OAUTH_CLIENT_BINDING_MISMATCH")
        scopes = set(str(info.get("scope", "")).split())
    if not scopes:
        raise RuntimeError("YOUTUBE_OAUTH_PREFLIGHT_FAILED: could not determine granted OAuth scopes")
    return scopes


def oauth_refresh_block(exc: BaseException) -> dict | None:
    """Convert non-retryable OAuth refresh failures into a durable owner-action state."""
    text = str(exc).lower()
    auth_markers = (
        "invalid_grant",
        "expired",
        "revoked",
        "unauthorized_client",
        "invalid_client",
        "deleted_client",
    )
    if not isinstance(exc, RefreshError) and not any(marker in text for marker in auth_markers):
        return None
    if not any(marker in text for marker in auth_markers):
        return None

    client_problem = any(
        marker in text
        for marker in ("unauthorized_client", "invalid_client", "deleted_client")
    )
    return {
        "requires_owner_action": True,
        "status": "PUBLISH_BLOCKED_AUTH",
        "action": (
            "UPDATE_YOUTUBE_OAUTH_CLIENT_AND_REAUTHORIZE"
            if client_problem
            else "REAUTHORIZE_YOUTUBE_OAUTH"
        ),
        "video_id": None,
        "uploaded": False,
        "reason": (
            "YouTube rejected the OAuth client. Ensure YOUTUBE_CLIENT_ID, "
            "YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN all belong to the same "
            "OAuth client, then reauthorize once. The approved episode remains ready "
            "to publish."
            if client_problem
            else "YouTube authorization expired or was revoked. Reauthorize once; "
            "the approved episode remains ready to publish."
        ),
    }


def capability_gap(mode: str, granted: set[str]) -> dict | None:
    missing = [name for name, accepted in MODE_REQUIREMENTS[mode] if not accepted & granted]
    if not missing:
        return None
    return {
        "requires_owner_action": True,
        "status": "OWNER_ACTION_REQUIRED",
        "action": "REAUTHORIZE_YOUTUBE_OAUTH",
        "mode": mode,
        "video_id": None,
        "uploaded": False,
        "reason": "OAuth token lacks required capability: " + ", ".join(missing),
        "missing_capabilities": missing,
        "granted_scopes": sorted(granted),
    }


def record_remote_upload(identifier: str, video_id: str, status: str) -> None:
    access = os.environ.get("IAS3_ACCESS_KEY", "")
    secret = os.environ.get("IAS3_SECRET_KEY", "")
    if not access or not secret:
        raise RuntimeError("Internet Archive credentials are required for atomic YouTube publishing")
    from archive_storage import advance_index_status, get_archive_item
    from error_classifier import retry_transient
    from state_machine import State

    canonical_state = State.PUBLISHED if status == "verified" else State.PUBLISHING
    try:
        advance_index_status(identifier, canonical_state, youtube_video_id=video_id)
    except Exception as exc:
        print(f"Warning: local catalogue update for {identifier} failed: {exc}")

    try:
        import state_store
        state_store.save_episode_for_archive(identifier, {
            "youtube_video_id": video_id,
            "youtube_upload_status": status,
        })
    except Exception as exc:
        print(f"Warning: automation-state update for {identifier} failed: {exc}")

    def _update():
        get_archive_item(identifier, access_key=access, secret_key=secret).modify_metadata({
            "youtube_video_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "youtube_upload_status": status,
        })
    retry_transient(_update)


def wait_until_processed(youtube, video_id: str) -> dict:
    from error_classifier import classify_error, ErrorCategory
    last = {}
    for attempt in range(20):
        try:
            response = youtube.videos().list(part="status,contentDetails,snippet", id=video_id).execute()
            items = response.get("items") or []
            if items:
                last = items[0]
                upload_status = last.get("status", {}).get("uploadStatus")
                if upload_status == "processed":
                    return last
                if upload_status in {"failed", "rejected", "deleted"}:
                    raise RuntimeError(f"YouTube processing failed: {upload_status}")
        except Exception as exc:
            if classify_error(exc) != ErrorCategory.TRANSIENT_RETRYABLE:
                raise exc
        if attempt < 19:
            time.sleep(15)
    raise RuntimeError(f"YouTube processing was not verified: {last}")


def remote_url(identifier: str, filename: str) -> str:
    return "https://archive.org/download/{}/{}".format(
        urllib.parse.quote(identifier, safe=""), urllib.parse.quote(filename)
    )


def download(identifier: str, filename: str, path: Path) -> None:
    with urllib.request.urlopen(remote_url(identifier, filename), timeout=600) as response, path.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)


def read_text(identifier: str, filename: str) -> str:
    with urllib.request.urlopen(remote_url(identifier, filename), timeout=60) as response:
        return response.read().decode("utf-8-sig").strip()


def credentials() -> Credentials:
    required = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing YouTube credential fields: " + ", ".join(missing))
    return Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
    )


def _record_remote_fields(identifier: str, fields: dict) -> bool:
    """Return whether at least one durable backend acknowledged the write."""
    saved = False
    try:
        import state_store
        saved = state_store.save_episode_for_archive(identifier, dict(fields)) is True
    except Exception as exc:
        print(f"Warning: automation-state update for {identifier} failed: {type(exc).__name__}")
    access, secret = os.environ.get("IAS3_ACCESS_KEY", ""), os.environ.get("IAS3_SECRET_KEY", "")
    if not access or not secret:
        return saved
    try:
        from archive_storage import get_archive_item
        from error_classifier import retry_transient
        response = retry_transient(lambda: get_archive_item(identifier, access_key=access, secret_key=secret).modify_metadata(dict(fields)))
        status = getattr(response, "status_code", None)
        if not saved and isinstance(status, int) and 200 <= status < 300:
            # IA can queue metadata writes. An HTTP acknowledgement alone must
            # not authorize insert if the intent is not readable durably yet.
            actual = archive_metadata(identifier).get("metadata") or {}
            saved = all(actual.get(key) == value for key, value in fields.items())
    except Exception as exc:
        print(f"Warning: archive metadata update for {identifier} failed: {type(exc).__name__}")
    return saved


def record_upload_intent(identifier: str, status: str) -> None:
    """Written BEFORE videos.insert: if the runner dies mid-upload the next run knows a video may exist."""
    if not _record_remote_fields(identifier, {"youtube_upload_status": status}):
        raise RuntimeError("YOUTUBE_UPLOAD_INTENT_NOT_DURABLE: upload not started")


def upload_intent_pending(identifier: str, metadata: dict) -> bool:
    if str(metadata.get("youtube_upload_status") or "") in UNCERTAIN_UPLOAD_STATES:
        return True
    try:
        import state_store
        for episode in (state_store.load_publish_state().get("episodes") or {}).values():
            if episode.get("archive_identifier") == identifier and episode.get("youtube_upload_status") in UNCERTAIN_UPLOAD_STATES:
                return True
    except Exception:
        raise RuntimeError("YOUTUBE_PUBLISH_STATE_UNAVAILABLE: no new upload allowed") from None
    return False


def set_thumbnail_safely(youtube, identifier: str, video_id: str, thumbnail: Path) -> bool:
    """The video already exists: a thumbnail problem is retried, recorded and never creates a second video."""
    from error_classifier import retry_transient
    media = MediaFileUpload(str(thumbnail), mimetype="image/jpeg")
    try:
        with media.stream():
            retry_transient(lambda: youtube.thumbnails().set(
                videoId=video_id, media_body=media).execute())
    except Exception as exc:
        print(f"Warning: YouTube thumbnail upload failed ({type(exc).__name__}); the video is kept: {video_id}")
        _record_remote_fields(identifier, {"youtube_thumbnail_status": "failed"})
        return False
    _record_remote_fields(identifier, {"youtube_thumbnail_status": "set"})
    return True


def retry_missing_thumbnail(youtube, identifier: str, video_id: str, metadata: dict) -> None:
    if str(metadata.get("youtube_thumbnail_status") or "") != "failed":
        return
    with tempfile.TemporaryDirectory(prefix="sleep-history-thumbnail-") as folder:
        thumbnail = Path(folder) / "thumbnail.jpg"
        try:
            download(identifier, "11_thumbnail_base.jpg", thumbnail)
        except Exception as exc:
            print(f"Warning: thumbnail retry could not download the image: {type(exc).__name__}")
            return
        set_thumbnail_safely(youtube, identifier, video_id, thumbnail)


def pending_youtube_record_path(identifier: str) -> Path:
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    return runtime / f"pending_youtube_{identifier}.json"


def get_pending_youtube_id(identifier: str) -> str:
    path = pending_youtube_record_path(identifier)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("youtube_video_id", "")).strip()
        except Exception:
            pass
    return ""


def save_pending_youtube_id(identifier: str, video_id: str) -> None:
    path = pending_youtube_record_path(identifier)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"youtube_video_id": video_id}, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def clear_pending_youtube_id(identifier: str) -> None:
    pending_youtube_record_path(identifier).unlink(missing_ok=True)


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def finalize_public(youtube, identifier: str, video_id: str, verified: dict, mode: str) -> dict:
    try:
        youtube.videos().update(
            part="status",
            body={"id": video_id, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}},
        ).execute()
    except HttpError as exc:
        if exc.resp.status in (403, 401):
            save_pending_youtube_id(identifier, video_id)
            record_remote_upload(identifier, video_id, "processing")
            return {
                "video_id": video_id,
                "requires_owner_action": True,
                "action": "CHANGE_PRIVACY_TO_PUBLIC",
                "mode": mode,
                "reason": f"OAuth scope insufficient for privacy change: {exc.resp.reason}",
                "url": watch_url(video_id),
                "status": "private_pending_owner",
            }
        raise
    record_remote_upload(identifier, video_id, "verified")
    return {"video_id": video_id, "url": watch_url(video_id), "already_published": False, "verified": verified, "mode": mode}


def resume_existing(youtube, identifier: str, video_id: str, mode: str, granted: set[str]) -> dict | None:
    """Continue publishing a video that is already on YouTube.

    An empty API result may mean a different channel/account, not deletion.
    An existing ID is never discarded automatically. API errors propagate.
    """
    if not granted & READ_SCOPES:
        # The ID may belong to a private upload from an earlier mode. Without
        # read access neither publication nor deletion can be asserted.
        return {
            "video_id": video_id,
            "url": watch_url(video_id),
            "requires_owner_action": True, "status": "OWNER_ACTION_REQUIRED",
            "action": "REAUTHORIZE_YOUTUBE_OAUTH_WITH_READ_SCOPE",
            "uploaded": False, "mode": mode,
            "reason": "The existing video is preserved; a read scope is needed to verify its state without uploading another copy.",
        }
    items = youtube.videos().list(part="status,contentDetails,snippet", id=video_id).execute().get("items") or []
    if not items or items[0].get("status", {}).get("uploadStatus") in {"failed", "rejected", "deleted"}:
        return {
            "requires_owner_action": True, "status": "OWNER_ACTION_REQUIRED",
            "action": "VERIFY_EXISTING_YOUTUBE_UPLOAD", "video_id": video_id,
            "uploaded": False, "mode": mode,
            "reason": "A saved YouTube ID is missing or unusable. Verify the owning channel and existing upload; no replacement is uploaded automatically.",
        }
    verified = wait_until_processed(youtube, video_id)
    if verified.get("status", {}).get("privacyStatus") == "public":
        record_remote_upload(identifier, video_id, "verified")
        return {
            "video_id": video_id,
            "url": watch_url(video_id),
            "already_published": True,
            "verified": verified,
            "mode": mode,
        }
    return finalize_public(youtube, identifier, video_id, verified, mode)


def find_existing_video_id(identifier: str, metadata: dict) -> str:
    existing = metadata.get("youtube_video_id") or get_pending_youtube_id(identifier)
    if existing:
        return existing
    for cfg_name in ("topics_history.json", "archive_index.json"):
        cfg_file = ROOT / "config" / cfg_name
        if not cfg_file.exists():
            continue
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for rec in data if isinstance(data, list) else []:
            if (rec.get("identifier") == identifier or rec.get("archive_identifier") == identifier
                    or (rec.get("slug") and identifier.endswith("-" + rec["slug"]))) and rec.get("youtube_video_id"):
                return rec["youtube_video_id"]
    try:
        import state_store
        for episode in (state_store.load_publish_state().get("episodes") or {}).values():
            if episode.get("archive_identifier") == identifier and episode.get("youtube_video_id"):
                return episode["youtube_video_id"]
    except Exception:
        raise RuntimeError("YOUTUBE_PUBLISH_STATE_UNAVAILABLE: no new upload allowed") from None
    return ""


def publish_archive_item(identifier: str) -> dict:
    mode = publish_mode()
    creds = credentials()
    # Capability gate: runs before any download, search or videos.insert.
    try:
        granted = get_granted_scopes(creds)
    except RefreshError as exc:
        blocked = oauth_refresh_block(exc)
        if blocked:
            return blocked
        raise
    blocked = capability_gap(mode, granted)
    if blocked:
        return blocked

    payload = archive_metadata(identifier)
    metadata = payload.get("metadata") or {}
    existing = find_existing_video_id(identifier, metadata)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    if existing:
        outcome = resume_existing(youtube, identifier, existing, mode, granted)
        if outcome is not None:
            if not outcome.get("requires_owner_action"):
                retry_missing_thumbnail(youtube, identifier, outcome.get("video_id") or existing, metadata)
            return outcome

    title = read_text(identifier, "01_title.txt")
    description = read_text(identifier, "02_description.txt")
    episode_metadata = json.loads(read_text(identifier, "15_metadata.json"))
    if episode_metadata.get("youtube_packaging_status"):
        expected_title = str(episode_metadata.get("final_youtube_title", "")).strip()
        thumbnail_text = str(episode_metadata.get("thumbnail_text", "")).strip()
        if episode_metadata.get("youtube_packaging_validation_status") != "passed":
            raise RuntimeError("YOUTUBE_PUBLISH_BLOCKED: packaging validation did not pass")
        if title != expected_title or title != str(episode_metadata.get("title", "")).strip():
            raise RuntimeError("YOUTUBE_PUBLISH_BLOCKED: approved title fields do not match")
        if not 2 <= len(re.findall(r"[\u0621-\u064A]+", thumbnail_text)) <= 4:
            raise RuntimeError("YOUTUBE_PUBLISH_BLOCKED: thumbnail text is invalid")
        if episode_metadata.get("thumbnail_text_render_status") != "passed":
            raise RuntimeError("YOUTUBE_PUBLISH_BLOCKED: exact Arabic thumbnail text was not rendered")

    # Hard-crash recovery: look for a video already uploaded with this exact title
    if not existing:
        discovered = []
        try:
            search_response = youtube.search().list(
                part="id,snippet", forMine=True, type="video", q=title, maxResults=5,
            ).execute()
            discovered = search_response.get("items") or []
        except Exception as search_exc:
            print(f"Warning: YouTube search discovery failed: {type(search_exc).__name__}")
        for item in discovered:
            item_title = item.get("snippet", {}).get("title", "").strip()
            item_id = item.get("id", {}).get("videoId", "")
            if item_id and item_title == title.strip():
                # Verification failure MUST propagate, not fall into a new insert.
                save_pending_youtube_id(identifier, item_id)
                return resume_existing(youtube, identifier, item_id, mode, granted)
        if upload_intent_pending(identifier, metadata):
            # A previous attempt started videos.insert and never recorded a video ID, and this token
            # cannot search YouTube to check. YouTube has no idempotency key, so never guess.
            return {
                "requires_owner_action": True,
                "status": "OWNER_ACTION_REQUIRED",
                "action": ("VERIFY_EXISTING_YOUTUBE_UPLOAD" if granted & READ_SCOPES
                           else "REAUTHORIZE_YOUTUBE_OAUTH_WITH_READ_SCOPE"),
                "mode": mode,
                "video_id": None,
                "uploaded": False,
                "reason": "A previous upload may have created the video. A failed or empty title search cannot prove absence; verify the channel before any new upload.",
            }

    with tempfile.TemporaryDirectory(prefix="sleep-history-youtube-") as folder:
        directory = Path(folder)
        video = directory / "video.mp4"
        thumbnail = directory / "thumbnail.jpg"
        download(identifier, "14_video_final.mp4", video)
        download(identifier, "11_thumbnail_base.jpg", thumbnail)
        if video.stat().st_size <= 1024:
            raise RuntimeError("Downloaded archive video is empty")

        privacy = "public" if mode == MODE_DIRECT_PUBLIC else "private"
        record_upload_intent(identifier, "inserting")
        media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
        with media.stream():
            request = youtube.videos().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": title, "description": description, "categoryId": "27", "defaultLanguage": "ar"},
                    "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
                },
                media_body=media,
            )
            response = None
            # Exceptions retain the durable 'inserting' fence, including 4xx:
            # an error cannot prove that no previous chunk/final request committed.
            while response is None:
                _, response = request.next_chunk()
        video_id = response.get("id")
        if not video_id:
            raise RuntimeError("YouTube upload finished without a video ID")
        save_pending_youtube_id(identifier, video_id)
        record_remote_upload(identifier, video_id, "processing")

        set_thumbnail_safely(youtube, identifier, video_id, thumbnail)

        if mode == MODE_DIRECT_PUBLIC and not granted & READ_SCOPES:
            record_remote_upload(identifier, video_id, "verified")
            return {
                "video_id": video_id,
                "url": watch_url(video_id),
                "already_published": False,
                "verified": {"verification": "skipped_insufficient_scope"},
                "mode": mode,
            }
        verified = wait_until_processed(youtube, video_id)
        if verified.get("status", {}).get("uploadStatus") != "processed":
            raise RuntimeError("YouTube upload processing failed or did not complete")

        if mode == MODE_DIRECT_PUBLIC:
            record_remote_upload(identifier, video_id, "verified")
            return {"video_id": video_id, "url": watch_url(video_id), "already_published": False, "verified": verified, "mode": mode}
        return finalize_public(youtube, identifier, video_id, verified, mode)
