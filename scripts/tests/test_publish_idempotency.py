from unittest.mock import MagicMock, patch
import json
from pathlib import Path
import pytest
import youtube_publish


def test_publish_archive_item_existing_video_id():
    mock_payload = {
        "metadata": {
            "youtube_video_id": "existing123",
        }
    }
    mock_youtube = MagicMock()
    mock_videos = MagicMock()
    mock_list = MagicMock()
    mock_list.execute.return_value = {
        "items": [{
            "status": {"uploadStatus": "processed", "privacyStatus": "public"}
        }]
    }
    mock_videos.list.return_value = mock_list
    mock_youtube.videos.return_value = mock_videos

    with patch("youtube_publish.archive_metadata", return_value=mock_payload), \
         patch("youtube_publish.build", return_value=mock_youtube), \
         patch("youtube_publish.credentials", return_value=MagicMock()), \
         patch("youtube_publish.record_remote_upload"):

        result = youtube_publish.publish_archive_item("item-123")

        assert result["video_id"] == "existing123"
        assert result["already_published"] is True


def test_publish_recovery_after_failure_post_youtube_insert(tmp_path, monkeypatch):
    mock_payload = {"metadata": {}}
    mock_youtube = MagicMock()
    mock_videos = MagicMock()
    mock_insert = MagicMock()
    mock_insert.next_chunk.return_value = (None, {"id": "new_yt_123"})
    mock_videos.insert.return_value = mock_insert

    mock_list = MagicMock()
    mock_list.execute.return_value = {
        "items": [{
            "status": {"uploadStatus": "processed", "privacyStatus": "public"}
        }]
    }
    mock_videos.list.return_value = mock_list

    mock_search = MagicMock()
    mock_search_list = MagicMock()
    mock_search_list.execute.return_value = {
        "items": [
            {
                "id": {"videoId": "new_yt_123"},
                "snippet": {"title": "title"}
            }
        ]
    }
    mock_search.list.return_value = mock_search_list
    mock_youtube.search.return_value = mock_search
    mock_youtube.videos.return_value = mock_videos

    def mock_download(identifier, filename, path):
        path.write_bytes(b"X" * 2000)

    def mock_read_text(identifier, filename):
        if filename == "15_metadata.json":
            return json.dumps({"title": "title"})
        return "title"

    # 1. Attempt 1: Upload succeeds, but record_remote_upload (IA metadata update) fails
    with patch("youtube_publish.archive_metadata", return_value=mock_payload), \
         patch("youtube_publish.build", return_value=mock_youtube), \
         patch("youtube_publish.credentials", return_value=MagicMock()), \
         patch("youtube_publish.download", side_effect=mock_download), \
         patch("youtube_publish.read_text", side_effect=mock_read_text), \
         patch("youtube_publish.record_remote_upload", side_effect=RuntimeError("IA update failed")):

        with pytest.raises(RuntimeError):
            youtube_publish.publish_archive_item("item-failure-test")

    # 2. Hard crash simulation: wipe local .runtime pending record
    pending_file = youtube_publish.pending_youtube_record_path("item-failure-test")
    if pending_file.exists():
        pending_file.unlink()

    # 3. Attempt 2: Fresh runner with empty .runtime, NO youtube_video_id in IA metadata, and NO local pending catalogue
    mock_videos.insert.reset_mock()
    with patch("youtube_publish.archive_metadata", return_value=mock_payload), \
         patch("youtube_publish.build", return_value=mock_youtube), \
         patch("youtube_publish.credentials", return_value=MagicMock()), \
         patch("youtube_publish.download", side_effect=mock_download), \
         patch("youtube_publish.read_text", side_effect=mock_read_text), \
         patch("youtube_publish.record_remote_upload"):

        res = youtube_publish.publish_archive_item("item-failure-test")
        assert res["video_id"] == "new_yt_123"
        assert res["already_published"] is True
        mock_videos.insert.assert_not_called()
