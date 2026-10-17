"""youtube_publish_mode branching and OAuth capability preflight (no network)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

import youtube_publish as yp

UPLOAD_ONLY = {yp.SCOPE_UPLOAD}
FULL = {yp.SCOPE_MANAGE}
METADATA = json.dumps({
    "youtube_packaging_status": "passed", "final_youtube_title": "title", "title": "title",
    "thumbnail_text": "بغداد ليلًا", "youtube_packaging_validation_status": "passed",
    "thumbnail_text_render_status": "passed",
})


def http_error(status):
    resp = httplib2.Response({"status": status})
    resp.reason = "Forbidden"
    return HttpError(resp, b"{}")


class Harness:
    def __init__(self, tmp_path, monkeypatch, mode, scopes, ia_metadata=None, video_state=None):
        self.youtube = MagicMock()
        self.videos = self.youtube.videos.return_value
        self.videos.insert.return_value.next_chunk.return_value = (None, {"id": "yt_new"})
        self.set_video(video_state or {"uploadStatus": "processed", "privacyStatus": "private"})
        self.youtube.search.return_value.list.return_value.execute.return_value = {"items": []}
        self.build = MagicMock(return_value=self.youtube)
        self.download = MagicMock(side_effect=lambda identifier, name, path: path.write_bytes(b"X" * 2000))
        self.records = []
        self.pending = tmp_path / "pending.json"
        self.patches = [
            patch.object(yp, "load_settings", return_value={"youtube_publish_mode": mode}),
            patch.object(yp, "ROOT", tmp_path),
            patch.object(yp, "credentials", return_value=MagicMock()),
            patch.object(yp, "get_granted_scopes", return_value=set(scopes)),
            patch.object(yp, "build", self.build),
            patch.object(yp, "archive_metadata", return_value={"metadata": ia_metadata or {}}),
            patch.object(yp, "download", self.download),
            patch.object(yp, "read_text", side_effect=lambda i, n: METADATA if n == "15_metadata.json" else "title"),
            patch.object(yp, "record_remote_upload", side_effect=lambda *a: self.records.append(a)),
            patch.object(yp, "pending_youtube_record_path", return_value=self.pending),
        ]

    def set_video(self, status):
        self.videos.list.return_value.execute.return_value = {"items": [{"status": status}]} if status else {"items": []}

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()

    def run(self):
        return yp.publish_archive_item("ia-item")

    @property
    def insert_privacy(self):
        return self.videos.insert.call_args.kwargs["body"]["status"]["privacyStatus"]


def harness(tmp_path, monkeypatch, mode, scopes, **kwargs):
    return Harness(tmp_path, monkeypatch, mode, scopes, **kwargs)


def test_publish_mode_comes_from_settings():
    with patch.object(yp, "load_settings", return_value={"youtube_publish_mode": "approved_direct_public"}):
        assert yp.publish_mode() == "approved_direct_public"
    with patch.object(yp, "load_settings", return_value={}):
        assert yp.publish_mode() == "private_verify_public"
    with patch.object(yp, "load_settings", return_value={"youtube_publish_mode": "bogus"}):
        with pytest.raises(RuntimeError):
            yp.publish_mode()


def test_repository_settings_select_a_supported_mode():
    assert yp.publish_mode() in yp.MODE_REQUIREMENTS


def test_private_mode_uploads_private_verifies_then_makes_public(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL) as h:
        result = h.run()
    assert h.insert_privacy == "private"
    update = h.videos.update.call_args.kwargs
    assert update["body"]["status"]["privacyStatus"] == "public"
    assert result["video_id"] == "yt_new" and result["mode"] == "private_verify_public"
    assert h.records[-1][2] == "verified"


def test_private_mode_checks_oauth_capability_before_videos_insert(tmp_path, monkeypatch):
    order = []
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL) as h:
        h.videos.insert.side_effect = lambda **kw: order.append("insert") or MagicMock(
            next_chunk=lambda: (None, {"id": "yt_new"}))
        with patch.object(yp, "get_granted_scopes", side_effect=lambda c: order.append("preflight") or set(FULL)):
            h.run()
    assert order == ["preflight", "insert"]


def test_insufficient_capability_never_calls_videos_insert(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", UPLOAD_ONLY) as h:
        result = h.run()
    assert result["requires_owner_action"] is True
    assert result["status"] == "OWNER_ACTION_REQUIRED"
    assert result["uploaded"] is False and result["video_id"] is None
    assert "videos.list and videos.update" in result["missing_capabilities"]
    h.videos.insert.assert_not_called()
    h.build.assert_not_called()
    h.download.assert_not_called()
    assert h.records == []
    assert not h.pending.exists()


def test_undeterminable_scopes_fail_closed_without_upload(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL) as h:
        with patch.object(yp, "get_granted_scopes", side_effect=RuntimeError("YOUTUBE_OAUTH_PREFLIGHT_FAILED")):
            with pytest.raises(RuntimeError, match="PREFLIGHT"):
                h.run()
    h.videos.insert.assert_not_called()


def test_direct_public_mode_uploads_public_and_never_updates_privacy(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "approved_direct_public", UPLOAD_ONLY) as h:
        result = h.run()
    assert h.insert_privacy == "public"
    h.videos.update.assert_not_called()
    assert result["video_id"] == "yt_new" and result["mode"] == "approved_direct_public"
    assert h.records[-1][2] == "verified"


def test_direct_public_mode_verifies_when_read_scope_is_available(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "approved_direct_public", FULL,
                 video_state={"uploadStatus": "processed", "privacyStatus": "public"}) as h:
        result = h.run()
    assert h.insert_privacy == "public"
    assert result["verified"]["status"]["uploadStatus"] == "processed"
    h.videos.update.assert_not_called()


def test_direct_public_mode_without_any_upload_scope_is_blocked(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "approved_direct_public", {"https://www.googleapis.com/auth/userinfo.email"}) as h:
        result = h.run()
    assert result["status"] == "OWNER_ACTION_REQUIRED"
    h.videos.insert.assert_not_called()


def test_privacy_change_denied_after_race_keeps_video_private_and_reports_owner_action(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL) as h:
        h.videos.update.return_value.execute.side_effect = http_error(403)
        result = h.run()
    assert result["requires_owner_action"] and result["video_id"] == "yt_new"
    assert result["action"] == "CHANGE_PRIVACY_TO_PUBLIC"
    assert h.records[-1][2] == "processing"


def test_resume_does_not_upload_again_when_private_video_already_exists(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL, ia_metadata={"youtube_video_id": "yt_old"}) as h:
        result = h.run()
    h.videos.insert.assert_not_called()
    assert h.videos.update.call_args.kwargs["body"]["id"] == "yt_old"
    assert result["video_id"] == "yt_old"


def test_resume_with_public_video_is_a_noop(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL, ia_metadata={"youtube_video_id": "yt_old"},
                 video_state={"uploadStatus": "processed", "privacyStatus": "public"}) as h:
        result = h.run()
    h.videos.insert.assert_not_called()
    h.videos.update.assert_not_called()
    assert result["already_published"] is True


def test_resume_after_scope_fix_reuses_pending_video_instead_of_uploading(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", UPLOAD_ONLY) as h:
        h.pending.write_text(json.dumps({"youtube_video_id": "yt_old"}), encoding="utf-8")
        assert h.run()["status"] == "OWNER_ACTION_REQUIRED"
        h.videos.insert.assert_not_called()
        with patch.object(yp, "get_granted_scopes", return_value=set(FULL)):
            result = h.run()
    h.videos.insert.assert_not_called()
    assert result["video_id"] == "yt_old"


def test_resume_direct_mode_with_upload_only_scope_never_uploads_a_second_copy(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "approved_direct_public", UPLOAD_ONLY, ia_metadata={"youtube_video_id": "yt_old"}) as h:
        result = h.run()
    h.videos.insert.assert_not_called()
    assert result["video_id"] == "yt_old" and result["already_published"] is True


def test_resume_uploads_only_when_youtube_confirms_video_is_gone(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL, ia_metadata={"youtube_video_id": "yt_old"}) as h:
        h.set_video(None)
        h.videos.list.return_value.execute.side_effect = [{"items": []}, {"items": [{"status": {"uploadStatus": "processed", "privacyStatus": "private"}}]}]
        result = h.run()
    assert h.videos.insert.call_count == 1
    assert result["video_id"] == "yt_new"


def test_resume_api_error_never_triggers_a_duplicate_upload(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL, ia_metadata={"youtube_video_id": "yt_old"}) as h:
        h.videos.list.return_value.execute.side_effect = http_error(500)
        with pytest.raises(HttpError):
            h.run()
    h.videos.insert.assert_not_called()


def test_hard_crash_discovery_by_title_prevents_duplicate_upload(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL) as h:
        h.youtube.search.return_value.list.return_value.execute.return_value = {
            "items": [{"id": {"videoId": "yt_found"}, "snippet": {"title": "title"}}]}
        result = h.run()
    h.videos.insert.assert_not_called()
    assert result["video_id"] == "yt_found"
