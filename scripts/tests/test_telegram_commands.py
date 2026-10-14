from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "cloudflare" / "telegram-worker.js"


class TelegramWorkerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKER.read_text(encoding="utf-8")

    def test_new_video_commands_dispatch_cloud_production(self):
        for phrase in ("/new", "/video", "/episode", "فيديو جديد", "حلقة جديدة"):
            with self.subTest(phrase=phrase):
                self.assertIn(f'"{phrase}"', self.source)
        self.assertIn('dispatch(env, "production.yml"', self.source)

    def test_review_callbacks_dispatch_only_known_actions(self):
        self.assertIn('["publish", "reject", "regenerate"]', self.source)
        self.assertIn('dispatch(env, "review.yml"', self.source)

    def test_telegram_webhook_is_restricted_to_owner(self):
        self.assertIn("TELEGRAM_WEBHOOK_SECRET", self.source)
        self.assertIn("TELEGRAM_CHAT_ID", self.source)
        self.assertIn("TELEGRAM_USER_ID", self.source)

    def test_child_voice_command_is_isolated_and_uses_cheap_model(self):
        self.assertIn('"/voice"', self.source)
        self.assertIn('"صوت:"', self.source)
        self.assertIn('gemini-3.8-flash-lite-tts', self.source)
        self.assertIn("GEMINI_CHILD_VOICE_ID", self.source)
        self.assertIn('const CHILD_AUDIO_FOLDER = "أصوات الطفلة"', self.source)

    def test_child_voice_uploads_wav_directly_without_video_dispatch(self):
        self.assertIn('"content-type": "audio/wav"', self.source)
        self.assertIn("ctx.waitUntil(createVoiceFile", self.source)
        self.assertIn("MAX_VOICE_TEXT_CHARS", self.source)

    def test_failures_do_not_expose_stack_or_secrets_to_telegram(self):
        self.assertNotIn("error.stack", self.source)
        self.assertNotIn("env.GITHUB_TOKEN,", self.source)
        self.assertIn("لم يتم تنفيذ نشر أو حذف", self.source)


if __name__ == "__main__":
    unittest.main()
