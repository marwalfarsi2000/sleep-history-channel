from unittest.mock import MagicMock, patch
import pytest

import archive_storage


def test_get_archive_item_uses_archive_session():
    with patch("archive_storage.ArchiveSession") as mock_session_cls:
        mock_session_instance = MagicMock()
        mock_item = MagicMock()
        mock_session_instance.get_item.return_value = mock_item
        mock_session_cls.return_value = mock_session_instance

        item = archive_storage.get_archive_item("test-identifier", "acc", "sec")

        mock_session_cls.assert_called_once_with(config={"s3": {"access": "acc", "secret": "sec"}})
        mock_session_instance.get_item.assert_called_once_with("test-identifier")
        assert item == mock_item
