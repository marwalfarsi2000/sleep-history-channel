from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cloud_pipeline


class CloudPipelineLoggingTests(unittest.TestCase):
    def test_failed_child_output_is_streamed_and_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            command = [
                sys.executable,
                "-c",
                "import sys; print('VISIBLE_STDOUT'); "
                "print('VISIBLE_STDERR', file=sys.stderr); raise SystemExit(7)",
            ]
            with mock.patch.object(cloud_pipeline, "ROOT", root):
                with contextlib.redirect_stdout(output):
                    with self.assertRaisesRegex(RuntimeError, r"Command failed \(7\)"):
                        cloud_pipeline.run(command, "failure.log")

            console = output.getvalue()
            saved = (root / "logs" / "failure.log").read_text(encoding="utf-8")
            for marker in ("VISIBLE_STDOUT", "VISIBLE_STDERR"):
                self.assertIn(marker, console)
                self.assertIn(marker, saved)


if __name__ == "__main__":
    unittest.main()
