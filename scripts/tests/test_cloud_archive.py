from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


fake_module = types.ModuleType("internetarchive")
fake_module.get_item = mock.Mock()
fake_module.upload = mock.Mock()
fake_module.delete = mock.Mock()
sys.modules.setdefault("internetarchive", fake_module)
archive_storage = importlib.import_module("archive_storage")


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


if __name__ == "__main__":
    unittest.main()

