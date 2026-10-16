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
         patch("youtube_publish.credentials", return_value=MagicMock()):

        result = youtube_publish.publish_archive_item("item-123")

        assert result["video_id"] == "existing123"
        assert result["already_published"] is True
