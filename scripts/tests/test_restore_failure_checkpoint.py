import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.restore_failure_checkpoint import latest_unfinished_story, safe_story_members


class RestoreFailureCheckpointTests(unittest.TestCase):
    def test_latest_unfinished_story_prefers_newest_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "topics_history.json").write_text(
                json.dumps([
                    {"status": "drafting", "story_path": "stories/old"},
                    {"status": "pending_approval", "story_path": "stories/done"},
                    {"status": "generating", "story_path": "stories/current"},
                ]),
                encoding="utf-8",
            )
            self.assertEqual(latest_unfinished_story(root), "stories/current")

    def test_only_matching_story_logs_are_restored(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("stories/current/logs/chapter_01.md", "ok")
            archive.writestr("stories/other/logs/chapter_01.md", "wrong")
            archive.writestr("stories/current/15_metadata.json", "wrong")
            archive.writestr("../escape.txt", "wrong")
        payload.seek(0)
        with zipfile.ZipFile(payload) as archive:
            names = [item.filename for item in safe_story_members(archive, "stories/current")]
        self.assertEqual(names, ["stories/current/logs/chapter_01.md"])


if __name__ == "__main__":
    unittest.main()
