import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from scripts import restore_failure_checkpoint
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

    def test_pending_approval_without_archive_is_still_unfinished(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "topics_history.json").write_text(
                json.dumps([
                    {
                        "status": "pending_approval",
                        "story_path": "stories/archived",
                        "archive_identifier": "archived-item",
                    },
                    {
                        "status": "pending_approval",
                        "story_path": "stories/upload-failed",
                    },
                ]),
                encoding="utf-8",
            )
            self.assertEqual(latest_unfinished_story(root), "stories/upload-failed")

    def test_only_matching_story_checkpoint_files_are_restored(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("stories/current/logs/chapter_01.md", "ok")
            archive.writestr("stories/current/tts_chunks/001.txt", "tts")
            archive.writestr("stories/current/audio_chunks/001.mp3", "audio")
            archive.writestr("stories/current/06_story_tts_annotated.md", "annotated")
            archive.writestr("stories/other/logs/chapter_01.md", "wrong")
            archive.writestr("stories/current/14_video_final.mp4", "wrong")
            archive.writestr("../escape.txt", "wrong")
        payload.seek(0)
        with zipfile.ZipFile(payload) as archive:
            names = [item.filename for item in safe_story_members(archive, "stories/current")]
        self.assertEqual(names, [
            "stories/current/logs/chapter_01.md",
            "stories/current/tts_chunks/001.txt",
            "stories/current/audio_chunks/001.mp3",
            "stories/current/06_story_tts_annotated.md",
        ])

    def test_artifact_redirect_does_not_forward_github_token(self):
        class RedirectingOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {"Location": "https://example.blob.core.windows.net/file.zip?sig=signed"},
                    None,
                )

        response = io.BytesIO(b"zip-bytes")
        with mock.patch.object(
            restore_failure_checkpoint.urllib.request,
            "build_opener",
            return_value=RedirectingOpener(),
        ), mock.patch.object(
            restore_failure_checkpoint.urllib.request,
            "urlopen",
            return_value=response,
        ) as redirected_open:
            result = restore_failure_checkpoint.download(
                "https://api.github.com/repos/o/r/actions/artifacts/1/zip",
                "secret-token",
            )

        self.assertEqual(result, b"zip-bytes")
        redirected_request = redirected_open.call_args.args[0]
        self.assertNotIn("Authorization", redirected_request.headers)
        self.assertEqual(redirected_request.full_url.split("?", 1)[0],
                         "https://example.blob.core.windows.net/file.zip")


if __name__ == "__main__":
    unittest.main()
