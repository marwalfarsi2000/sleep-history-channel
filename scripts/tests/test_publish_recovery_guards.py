"""Offline regressions for uncertain uploads; never contact YouTube or state origin."""
from unittest.mock import patch

import pytest
import state_store
import youtube_publish as yp
from test_youtube_publish_modes import FULL, harness, http_error


@pytest.mark.parametrize("discovery", ["empty", "unavailable"])
def test_uncertain_prior_insert_never_reuploads(tmp_path, monkeypatch, discovery):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL,
                 ia_metadata={"youtube_upload_status": "inserting"}) as h:
        if discovery == "unavailable":
            h.youtube.search.return_value.list.return_value.execute.side_effect = http_error(503)
        result = h.run()
    h.videos.insert.assert_not_called()
    h.download.assert_not_called()
    assert result["requires_owner_action"]


def test_discovered_video_verification_error_never_falls_through_to_insert(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL) as h:
        h.youtube.search.return_value.list.return_value.execute.return_value = {
            "items": [{"id": {"videoId": "existing"}, "snippet": {"title": "title"}}]}
        h.videos.list.return_value.execute.side_effect = http_error(503)
        with pytest.raises(yp.HttpError):
            h.run()
    h.videos.insert.assert_not_called()


def test_insert_requires_a_durable_intent_ack(tmp_path, monkeypatch):
    monkeypatch.delenv("IAS3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("IAS3_SECRET_KEY", raising=False)
    with patch.object(state_store, "save_episode_for_archive", return_value=False):
        with pytest.raises(RuntimeError, match="YOUTUBE_UPLOAD_INTENT_NOT_DURABLE"):
            yp.record_upload_intent("episode", "inserting")


def test_expired_auth_does_not_remove_saved_video_id(tmp_path, monkeypatch):
    import cloud_pipeline as cp
    blocked = {"requires_owner_action": True, "status": "PUBLISH_BLOCKED_AUTH",
               "action": "REAUTHORIZE_YOUTUBE_OAUTH", "video_id": None}
    with patch.object(cp, "remote_status", return_value="PUBLISHING"), \
         patch.object(cp, "archive_metadata", return_value={"metadata": {
             "approval_status": "PUBLISHING", "youtube_video_id": "existing"}}), \
         patch("archive_recovery.ensure_index_record"), \
         patch.object(yp, "publish_archive_item", return_value=blocked), \
         patch.object(cp, "persist_state") as persist, \
         patch.object(cp, "modify_remote"), patch.object(cp, "send_message"):
        cp.publish("episode")
    assert all(call.kwargs.get("youtube_video_id", "existing") == "existing"
               for call in persist.call_args_list)


def test_unknown_fresh_state_never_authorizes_insert(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL) as h:
        with patch.object(state_store, "load_publish_state", side_effect=RuntimeError("unavailable")):
            with pytest.raises(RuntimeError, match="YOUTUBE_PUBLISH_STATE_UNAVAILABLE"):
                h.run()
    h.videos.insert.assert_not_called()
    h.download.assert_not_called()


def test_fresh_state_does_not_fall_back_to_stale_tracking_ref():
    from types import SimpleNamespace
    with patch.object(state_store, "_git", return_value=SimpleNamespace(returncode=1)), \
         patch.object(state_store, "_tip", return_value="old-cached-tip") as old:
        with pytest.raises(state_store.StateStoreError):
            state_store.load_publish_state()
    old.assert_not_called()


def test_late_upload_4xx_keeps_intent_instead_of_claiming_no_video(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL) as h:
        h.videos.insert.return_value.next_chunk.side_effect = http_error(403)
        with patch.object(yp, "record_upload_intent") as intent:
            with pytest.raises(yp.HttpError):
                h.run()
    intent.assert_called_once_with("ia-item", "inserting")


def test_confirmed_intent_can_use_either_durable_backend(monkeypatch):
    monkeypatch.delenv("IAS3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("IAS3_SECRET_KEY", raising=False)
    with patch.object(state_store, "save_episode_for_archive", return_value=True):
        yp.record_upload_intent("episode", "inserting")


def test_archive_error_response_is_not_an_ack(monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setenv("IAS3_ACCESS_KEY", "TEST_ONLY")
    monkeypatch.setenv("IAS3_SECRET_KEY", "TEST_ONLY")
    item = MagicMock()
    item.modify_metadata.return_value.status_code = 403
    with patch.object(state_store, "save_episode_for_archive", return_value=False), \
         patch("archive_storage.get_archive_item", return_value=item):
        with pytest.raises(RuntimeError, match="YOUTUBE_UPLOAD_INTENT_NOT_DURABLE"):
            yp.record_upload_intent("episode", "inserting")


def test_legacy_rejected_marker_is_not_proof_that_no_video_exists(tmp_path, monkeypatch):
    with harness(tmp_path, monkeypatch, "private_verify_public", FULL,
                 ia_metadata={"youtube_upload_status": "rejected"}) as h:
        result = h.run()
    h.videos.insert.assert_not_called()
    assert result["requires_owner_action"]


@pytest.mark.parametrize("visible,allowed", [("inserting", True), (None, False)])
def test_archive_only_ack_requires_readback(monkeypatch, visible, allowed):
    from unittest.mock import MagicMock
    monkeypatch.setenv("IAS3_ACCESS_KEY", "TEST_ONLY")
    monkeypatch.setenv("IAS3_SECRET_KEY", "TEST_ONLY")
    item = MagicMock()
    item.modify_metadata.return_value.status_code = 200
    with patch.object(state_store, "save_episode_for_archive", return_value=False), \
         patch("archive_storage.get_archive_item", return_value=item), \
         patch.object(yp, "archive_metadata", return_value={"metadata": {"youtube_upload_status": visible}}):
        if allowed:
            yp.record_upload_intent("episode", "inserting")
        else:
            with pytest.raises(RuntimeError, match="YOUTUBE_UPLOAD_INTENT_NOT_DURABLE"):
                yp.record_upload_intent("episode", "inserting")
