from unittest.mock import MagicMock, patch
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
    mock_youtube.videos.return_value = mock_videos

    def mock_download(identifier, filename, path):
        path.write_bytes(b"X" * 2000)

    # Simulate failure on record_remote_upload during first run
    with patch("youtube_publish.archive_metadata", return_value=mock_payload), \
         patch("youtube_publish.build", return_value=mock_youtube), \
         patch("youtube_publish.credentials", return_value=MagicMock()), \
         patch("youtube_publish.download", side_effect=mock_download), \
         patch("youtube_publish.read_text", return_value="title"), \
         patch("youtube_publish.record_remote_upload", side_effect=RuntimeError("IA update failed")):

        with pytest.raises(RuntimeError):
            youtube_publish.publish_archive_item("item-failure-test")

    # Prove that second run recovers using local pending ID and does NOT call insert again
    mock_videos.insert.reset_mock()
    with patch("youtube_publish.archive_metadata", return_value=mock_payload), \
         patch("youtube_publish.build", return_value=mock_youtube), \
         patch("youtube_publish.credentials", return_value=MagicMock()), \
         patch("youtube_publish.record_remote_upload"):

        res = youtube_publish.publish_archive_item("item-failure-test")
        assert res["video_id"] == "new_yt_123"
        assert res["already_published"] is True
        mock_videos.insert.assert_not_called()
