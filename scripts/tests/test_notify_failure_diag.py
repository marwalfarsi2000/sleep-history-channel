from pathlib import Path
from unittest.mock import patch
import notify_failure


def test_telegram_diagnostic_summary_extraction(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "cloud-generate.log"
    log_file.write_text("""
Traceback (most recent call last):
  File "generate_daily.py", line 120, in _gemini_one
    HTTPError: HTTP 400 Bad Request
    {
      "error": {
        "code": 400,
        "message": "Thinking level MINIMAL is not supported for this model. Please retry with other thinking level.",
        "status": "INVALID_ARGUMENT"
      }
    }
""", encoding="utf-8")

    with patch("notify_failure.ROOT", tmp_path):
        summary = notify_failure.diagnostic_summary()
        assert "Thinking level MINIMAL is not supported for this model." in summary
