"""approved_direct_public after the owner's Telegram approval (no network, no real APIs)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import archive_storage
import cloud_pipeline
import youtube_publish as yp

REPO = Path(__file__).resolve().parents[2]
IDENTIFIER = "lan-tukmil-alqissa-20261018-daily-life-in-palmyra-rom"
UPLOAD_ONLY = {yp.SCOPE_UPLOAD}
METADATA = json.dumps({
    "youtube_packaging_status": "passed", "final_youtube_title": "title", "title": "title",
    "thumbnail_text": "تدمر ليلًا", "youtube_packaging_validation_status": "passed",
    "thumbnail_text_render_status": "passed",
})


def test_repository_uses_approved_direct_public_mode():
    settings = json.loads((REPO / "config" / "settings.json").read_text(encoding="utf-8"))
    assert settings["youtube_publish_mode"] == "approved_direct_public"
    assert yp.publish_mode() == "approved_direct_public"


def test_current_upload_only_token_passes_direct_preflight_without_videos_update():
    assert yp.capability_gap("approved_direct_public", UPLOAD_ONLY) is None
    names = [name for name, _ in yp.MODE_REQUIREMENTS["approved_direct_public"]]
    assert names == ["videos.insert"]


def test_private_verify_public_still_requires_manage_scope_for_future_use():
    gap = yp.capability_gap("private_verify_public", UPLOAD_ONLY)
    assert gap["status"] == "OWNER_ACTION_REQUIRED"
    assert yp.capability_gap("private_verify_public", {yp.SCOPE_MANAGE}) is None


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    index = tmp_path / "config" / "archive_index.json"
    index.write_text(json.dumps([{"identifier": IDENTIFIER, "slug": "daily-life-in-palmyra-roman-era",
                                  "status": "REVIEW_PENDING"}]), encoding="utf-8")
    (tmp_path / "config" / "topics_history.json").write_text(json.dumps([
        {"slug": "daily-life-in-palmyra-roman-era", "archive_identifier": IDENTIFIER, "status": "REVIEW_PENDING"}
    ]), encoding="utf-8")
    (tmp_path / "config" / "settings.json").write_text(
        (REPO / "config" / "settings.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(archive_storage, "ROOT", tmp_path)
    monkeypatch.setattr(archive_storage, "ARCHIVE_INDEX", index)
    monkeypatch.setattr(yp, "ROOT", tmp_path)
    return tmp_path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_publish(catalogue, monkeypatch, remote="REVIEW_PENDING", scopes=UPLOAD_ONLY):
    youtube = MagicMock()
    videos = youtube.videos.return_value
    videos.insert.return_value.next_chunk.return_value = (None, {"id": "yt_palmyra"})
    youtube.search.return_value.list.return_value.execute.side_effect = Exception("insufficient scope")
    ia_item = MagicMock()
    ia_item.modify_metadata.return_value = MagicMock(status_code=200)
    sent, remote_updates = [], []
    metadata = {"title": "title", "approval_status": remote}
    monkeypatch.setenv("IAS3_ACCESS_KEY", "k")
    monkeypatch.setenv("IAS3_SECRET_KEY", "s")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    patches = [
        patch.object(yp, "credentials", return_value=MagicMock()),
        patch.object(yp, "get_granted_scopes", return_value=set(scopes)),
        patch.object(yp, "build", return_value=youtube),
        patch.object(yp, "archive_metadata", return_value={"metadata": metadata}),
        patch.object(yp, "download", side_effect=lambda i, n, path: path.write_bytes(b"X" * 2000)),
        patch.object(yp, "read_text", side_effect=lambda i, n: METADATA if n == "15_metadata.json" else "title"),
        patch.object(yp, "pending_youtube_record_path", return_value=catalogue / "pending.json"),
        patch.object(archive_storage, "get_archive_item", return_value=ia_item),
        patch.object(cloud_pipeline, "remote_status", return_value=remote),
        patch.object(cloud_pipeline, "archive_metadata", return_value={"metadata": metadata}),
        patch.object(cloud_pipeline, "modify_remote", side_effect=lambda i, f: remote_updates.append(f)),
        patch.object(cloud_pipeline, "send_message", side_effect=lambda *a, **k: sent.append(a)),
        patch("archive_recovery.ensure_index_record", return_value=True),
    ]
    for p in patches:
        p.start()
    try:
        return cloud_pipeline.publish(IDENTIFIER), youtube, ia_item, sent, remote_updates
    finally:
        for p in patches:
            p.stop()


def test_owner_approved_publish_uploads_public_with_upload_only_token_and_finishes_published(catalogue, monkeypatch):
    result, youtube, ia_item, sent, remote_updates = run_publish(catalogue, monkeypatch)
    videos = youtube.videos.return_value
    assert videos.insert.call_count == 1
    assert videos.insert.call_args.kwargs["body"]["status"]["privacyStatus"] == "public"
    videos.update.assert_not_called()
    assert result["video_id"] == "yt_palmyra" and result["url"].endswith("yt_palmyra")
    record = read_json(catalogue / "config" / "archive_index.json")[0]
    assert record["status"] == "PUBLISHED" and record["youtube_video_id"] == "yt_palmyra"
    assert read_json(catalogue / "config" / "topics_history.json")[0]["status"] == "PUBLISHED"
    assert remote_updates[-1]["approval_status"] == "PUBLISHED"
    assert remote_updates[-1]["youtube_video_id"] == "yt_palmyra"
    uploads = [call.args[0] for call in ia_item.modify_metadata.call_args_list]
    assert any(u.get("youtube_video_id") == "yt_palmyra" for u in uploads)
    assert len(sent) == 1


@pytest.mark.parametrize("remote", ["GENERATING", "MEDIA_READY", "REJECTED", "FAILED_RETRYABLE"])
def test_no_youtube_upload_before_owner_approval(catalogue, monkeypatch, remote):
    with patch("youtube_publish.publish_archive_item") as upload:
        monkeypatch.setattr(cloud_pipeline, "remote_status", lambda identifier: remote)
        with pytest.raises(RuntimeError, match="HUMAN_APPROVAL_GATE_FAILED"):
            cloud_pipeline.publish(IDENTIFIER)
    upload.assert_not_called()


def test_production_workflow_never_publishes_to_youtube():
    production = (REPO / ".github" / "workflows" / "production.yml").read_text(encoding="utf-8")
    assert "cloud_pipeline.py publish" not in production
    assert "YOUTUBE_REFRESH_TOKEN" not in production
    review = (REPO / ".github" / "workflows" / "review.yml").read_text(encoding="utf-8")
    assert 'cloud_pipeline.py "${{ inputs.action }}"' in review


def test_advance_index_status_walks_the_state_machine(catalogue):
    archive_storage.advance_index_status(IDENTIFIER, "PUBLISHED", youtube_video_id="abc")
    assert read_json(catalogue / "config" / "archive_index.json")[0]["status"] == "PUBLISHED"
    archive_storage.advance_index_status(IDENTIFIER, "PUBLISHED", youtube_url="u")
    assert read_json(catalogue / "config" / "archive_index.json")[0]["youtube_video_id"] == "abc"


def test_advance_index_status_refuses_unapproved_episodes(catalogue):
    index = catalogue / "config" / "archive_index.json"
    records = read_json(index)
    records[0]["status"] = "MEDIA_READY"
    index.write_text(json.dumps(records), encoding="utf-8")
    with pytest.raises(ValueError):
        archive_storage.advance_index_status(IDENTIFIER, "PUBLISHED")
