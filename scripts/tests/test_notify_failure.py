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

    def test_extracts_gemini_thinking_level_error_from_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            log_content = '''working
Gemini HTTP 400: {"error":{"status":"INVALID_ARGUMENT","message":"Thinking level MINIMAL is not supported for this model. Please retry with other thinking level."}}
RuntimeError: Gemini HTTP 400: INVALID_ARGUMENT'''
            (root / "logs" / "cloud-generate.log").write_text(log_content, encoding="utf-8")
            with mock.patch.object(notify_failure, "ROOT", root):
                summary = notify_failure.diagnostic_summary()
                self.assertIn("Thinking level MINIMAL is not supported", summary)
                self.assertNotIn("RuntimeError", summary)

    def test_extracts_generic_http_error_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            log_content = '''working
Gemini HTTP 503: {"error":{"status":"UNAVAILABLE","message":"Service unavailable"}}'''
            (root / "logs" / "cloud-generate.log").write_text(log_content, encoding="utf-8")
            with mock.patch.object(notify_failure, "ROOT", root):
                summary = notify_failure.diagnostic_summary()
                self.assertIn("Service unavailable", summary)
                self.assertNotIn('"error":', summary)

    def test_redacts_secrets_from_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            log_content = '''working
RuntimeError: API_KEY=sk-12345 TOKEN=abc123 authorization=Bearer xyz
Some error occurred'''
            (root / "logs" / "cloud-generate.log").write_text(log_content, encoding="utf-8")
            with mock.patch.object(notify_failure, "ROOT", root):
                summary = notify_failure.diagnostic_summary()
                self.assertIn("API_KEY=[REDACTED]", summary)
                self.assertIn("TOKEN=[REDACTED]", summary)
                self.assertIn("authorization=[REDACTED]", summary)
                self.assertNotIn("sk-12345", summary)
                self.assertNotIn("abc123", summary)


if __name__ == "__main__":
    unittest.main()
