from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import archive_storage


def test_delete_rejected_uses_per_file_delete_and_only_owned_files():
    owned = "14_video_final.mp4"
    derived = "14_video_final.mp4_itemimage.png"
    unrelated = "notes-from-archive.txt"

    response = SimpleNamespace(status_code=200)
    file_obj = MagicMock()
    file_obj.delete.return_value = response

    item = MagicMock()
    item.item_metadata = {
        "files": [
            {"name": owned},
            {"name": derived},
            {"name": unrelated},
        ]
    }
    item.get_file.return_value = file_obj

    with patch("archive_storage.credentials", return_value=("access", "secret")), patch(
        "archive_storage.get_archive_item", return_value=item
    ):
        archive_storage.delete_rejected("example-item")

    item.get_file.assert_called_once_with(owned)
    file_obj.delete.assert_called_once_with(
        cascade_delete=True,
        access_key="access",
        secret_key="secret",
        headers={"x-archive-keep-old-version": "0"},
    )


def test_delete_rejected_is_idempotent_when_owned_files_are_already_gone():
    item = MagicMock()
    item.item_metadata = {"files": [{"name": "__ia_thumb.jpg"}, {"name": "unrelated.txt"}]}

    with patch("archive_storage.credentials", return_value=("access", "secret")), patch(
        "archive_storage.get_archive_item", return_value=item
    ):
        archive_storage.delete_rejected("example-item")

    item.get_file.assert_not_called()
