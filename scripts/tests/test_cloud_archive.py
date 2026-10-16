from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


import archive_storage


class ArchiveStorageTests(unittest.TestCase):
    def test_identifier_fits_telegram_callback_limit(self):
        identifier = archive_storage.safe_identifier(
            "daily-life-in-caliphate-cordoba-with-a-very-long-name",
            "2026-10-12T02:00:00+04:00",
        )
        self.assertLessEqual(len(("regenerate:" + identifier).encode("utf-8")), 64)
        self.assertRegex(identifier, r"^[a-z0-9-]+$")

    def test_public_url_quotes_filename(self):
        url = archive_storage.public_file_url("episode-id", "video final.mp4")
        self.assertEqual(url, "https://archive.org/download/episode-id/video%20final.mp4")

    def test_index_update_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "archive_index.json"
            with mock.patch.object(archive_storage, "ARCHIVE_INDEX", path):
                archive_storage.update_index({"identifier": "one", "status": "pending_approval"})
                archive_storage.update_index({"identifier": "one", "status": "published"})
                records = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "published")

    def test_verify_upload_accepts_direct_file_before_metadata_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "14_video_final.mp4"
            video.write_bytes(b"video")
            response = mock.MagicMock()
            response.__enter__.return_value.headers = {"Content-Length": "5"}
            metadata = {
                "files": [{
                    "name": "14_video_final.mp4",
                    "size": "5",
                    "md5": "421b47ffd946ca083b65cd668c6b17e6",
                }]
            }
            with mock.patch.object(archive_storage, "archive_metadata", return_value=metadata), \
                 mock.patch.object(archive_storage.urllib.request, "urlopen", return_value=response):
                verified = archive_storage.verify_upload("episode-id", video)
            self.assertEqual(verified["size"], 5)

    def test_verification_retries_transient_archive_404(self):
        missing = urllib.error.HTTPError(
            "https://archive.org/download/episode-id/14_video_final.mp4",
            404,
            "Not Found",
            {},
            None,
        )
        ready = {"files": [{"name": "14_video_final.mp4", "size": "5", "sha256": "abc"}]}
        with mock.patch.object(
            archive_storage,
            "archive_metadata",
            side_effect=[{"files": []}, ready],
        ), mock.patch.object(
            archive_storage.urllib.request,
            "urlopen",
            side_effect=missing,
        ), mock.patch.object(archive_storage.time, "sleep") as sleep:
            verified = archive_storage.wait_for_remote_file(
                "episode-id",
                "14_video_final.mp4",
                attempts=2,
                delay_seconds=1,
            )
        self.assertEqual(verified["size"], "5")
        sleep.assert_called_once_with(1)

    def test_verification_waits_for_replaced_archive_file(self):
        old = {"files": [{"name": "14_video_final.mp4", "size": "4", "sha256": "old"}]}
        new = {"files": [{"name": "14_video_final.mp4", "size": "5", "sha256": "new"}]}
        head = mock.MagicMock()
        head.__enter__.return_value.headers = {"Content-Length": "4"}
        with mock.patch.object(
            archive_storage,
            "archive_metadata",
            side_effect=[old, new],
        ), mock.patch.object(
            archive_storage.urllib.request,
            "urlopen",
            return_value=head,
        ), mock.patch.object(archive_storage.time, "sleep") as sleep:
            verified = archive_storage.wait_for_remote_file(
                "episode-id",
                "14_video_final.mp4",
                attempts=2,
                delay_seconds=1,
                expected_size=5,
                expected_sha256="new",
            )
        self.assertEqual(verified["size"], "5")
        self.assertEqual(verified["sha256"], "new")
        sleep.assert_called_once_with(1)

    def test_verification_rejects_same_size_previous_content(self):
        old = {"files": [{"name": "14_video_final.mp4", "size": "5", "md5": "old"}]}
        new = {"files": [{"name": "14_video_final.mp4", "size": "5", "md5": "new"}]}
        head = mock.MagicMock()
        head.__enter__.return_value.headers = {"Content-Length": "5"}
        with mock.patch.object(
            archive_storage,
            "archive_metadata",
            side_effect=[old, new],
        ), mock.patch.object(
            archive_storage.urllib.request,
            "urlopen",
            return_value=head,
        ), mock.patch.object(archive_storage.time, "sleep") as sleep:
            verified = archive_storage.wait_for_remote_file(
                "episode-id",
                "14_video_final.mp4",
                attempts=2,
                delay_seconds=1,
                expected_size=5,
                expected_md5="new",
            )
        self.assertEqual(verified["md5"], "new")
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()

