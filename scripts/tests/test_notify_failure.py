from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import notify_failure


class FailureNotificationTests(unittest.TestCase):
    def test_reports_failed_production_stage_and_last_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            (root / "logs" / "cloud-generate.log").write_text(
                "working\nRuntimeError: Gemini HTTP 503: UNAVAILABLE\n",
                encoding="utf-8",
            )
            with mock.patch.object(notify_failure, "ROOT", root), mock.patch.dict(
                os.environ,
                {"INSTALL_OUTCOME": "success", "RESTORE_OUTCOME": "success", "PRODUCE_OUTCOME": "failure"},
                clear=False,
            ):
                self.assertIn("إنشاء القصة", notify_failure.failed_stage())
                self.assertIn("HTTP 503", notify_failure.diagnostic_summary())


if __name__ == "__main__":
    unittest.main()
