from __future__ import annotations

import unittest
import json
from unittest import mock

import telegram_bridge


class TelegramCommandTests(unittest.TestCase):
    def test_traceback_is_not_sent_to_user(self):
        message = telegram_bridge.user_safe_failure_message(
            "Traceback (most recent call last):\nsecret internal stack"
        )
        self.assertNotIn("Traceback", message)
        self.assertIn("آخر نقطة ناجحة", message)

    def test_manual_request_explicitly_requests_new_topic(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"pipeline_status": "busy"}).encode("utf-8")
        response.__enter__.return_value = response
        with mock.patch.object(telegram_bridge.urllib.request, "urlopen", return_value=response) as urlopen, \
             mock.patch.object(telegram_bridge, "send_message"):
            telegram_bridge.run_requested_video("123")
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"mode": "new"})

    def test_new_video_commands(self):
        accepted = [
            "/new",
            "/new@YouWontFinishTheStory_bot",
            "/video",
            "/episode",
            "فيديو جديد",
            "حلقة جديدة",
            "أنشئ فيديو جديد",
            "new video",
        ]
        for text in accepted:
            with self.subTest(text=text):
                self.assertTrue(telegram_bridge.is_new_video_command(text))
        self.assertFalse(telegram_bridge.is_new_video_command("اعتماد"))

    def test_authorized_message_starts_pipeline(self):
        update = {
            "message": {
                "text": "فيديو جديد",
                "chat": {"id": 123},
                "from": {"id": 456},
            }
        }
        with mock.patch.object(
            telegram_bridge,
            "load_secrets",
            return_value={"TELEGRAM_CHAT_ID": "123", "TELEGRAM_USER_ID": "456"},
        ), mock.patch.object(telegram_bridge, "start_requested_video") as start:
            telegram_bridge.handle_update(update)
        start.assert_called_once_with("123")

    def test_unauthorized_message_is_ignored(self):
        update = {
            "message": {
                "text": "/new",
                "chat": {"id": 999},
                "from": {"id": 888},
            }
        }
        with mock.patch.object(
            telegram_bridge,
            "load_secrets",
            return_value={"TELEGRAM_CHAT_ID": "123", "TELEGRAM_USER_ID": "456"},
        ), mock.patch.object(telegram_bridge, "start_requested_video") as start:
            telegram_bridge.handle_update(update)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
