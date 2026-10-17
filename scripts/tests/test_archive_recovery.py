import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import archive_storage
import cloud_pipeline


def test_ensure_index_record_rebuilds_missing_entry(tmp_path):
    index_file = tmp_path / "config" / "archive_index.json"
    history_file = tmp_path / "config" / "topics_history.json"
    index_file.parent.mkdir(parents=True)
    index_file.write_text("[]", encoding="utf-8")
    history = [{"slug": "palmyra", "archive_identifier": "lan-tukmil-palmyra", "status": "generating"}]
    history_file.write_text(json.dumps(history), encoding="utf-8")

    mock_remote_meta = {
        "metadata": {
            "slug": "palmyra",
            "title": "الحياة اليومية في تدمر الرومانية | قصة تاريخية للنوم 😴",
            "date": "2026-10-17",
            "approval_status": "REVIEW_PENDING",
        }
    }

    with patch.object(archive_storage, "ARCHIVE_INDEX", index_file), \
         patch.object(archive_storage, "ROOT", tmp_path), \
         patch("archive_storage.archive_metadata", return_value=mock_remote_meta):

        rec = archive_storage.ensure_index_record("lan-tukmil-palmyra")
        assert rec["identifier"] == "lan-tukmil-palmyra"
        assert rec["slug"] == "palmyra"

        saved_index = json.loads(index_file.read_text(encoding="utf-8"))
        assert len(saved_index) == 1
        assert saved_index[0]["identifier"] == "lan-tukmil-palmyra"

        saved_history = json.loads(history_file.read_text(encoding="utf-8"))
        assert saved_history[0]["status"] == "REVIEW_PENDING"


def test_publish_preflight_recovers_catalogue_before_side_effects(tmp_path):
    with patch("archive_storage.ensure_index_record", side_effect=RuntimeError("Remote item missing")), \
         patch("cloud_pipeline.modify_remote") as mock_modify, \
         patch("youtube_publish.publish_archive_item") as mock_yt:

        with pytest.raises(RuntimeError):
            cloud_pipeline.publish("missing-identifier")

        mock_modify.assert_not_called()
        mock_yt.assert_not_called()
