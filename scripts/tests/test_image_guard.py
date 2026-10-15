from __future__ import annotations

import json
import base64
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import image_generate
from PIL import Image


class ImageCostGuardTests(unittest.TestCase):
    def test_cloudflare_sends_original_prompt_and_supported_fields_only(self):
        settings = dict(image_generate.SETTINGS)
        try:
            image_generate.SETTINGS["image_model"] = "@cf/black-forest-labs/flux-1-schnell"
            image_generate.SETTINGS["image_steps"] = 4
            buffer = io.BytesIO()
            Image.new("RGB", (32, 32), "brown").save(buffer, format="JPEG")
            response_payload = {
                "success": True,
                "result": {"image": base64.b64encode(buffer.getvalue()).decode("ascii")},
            }
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(response_payload).encode("utf-8")
            with mock.patch("urllib.request.urlopen", return_value=response) as urlopen, mock.patch.object(
                image_generate, "safe_log"
            ):
                image_bytes, _ = image_generate.generate_cloudflare(
                    "ORIGINAL DYNAMIC PROMPT", "account-id", "token"
                )
            request = urlopen.call_args.args[0]
            sent = json.loads(request.data.decode("utf-8"))
            self.assertEqual(sent, {"prompt": "ORIGINAL DYNAMIC PROMPT", "steps": 4})
            self.assertTrue(image_bytes.startswith(b"\xff\xd8\xff"))
            self.assertNotIn("token", request.full_url)
        finally:
            image_generate.SETTINGS.clear()
            image_generate.SETTINGS.update(settings)

    def test_cloudflare_rejects_missing_or_invalid_base64(self):
        with self.assertRaisesRegex(RuntimeError, "missing image/base64"):
            image_generate._extract_cloudflare_image({"success": True, "result": {}})
        with self.assertRaisesRegex(RuntimeError, "invalid image/base64"):
            image_generate._extract_cloudflare_image(
                {"success": True, "result": {"image": "not-base64!"}}
            )

    def test_cloudflare_handles_authentication_failure(self):
        body = io.BytesIO(
            json.dumps({"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}).encode("utf-8")
        )
        error = urllib.error.HTTPError("https://api.cloudflare.com", 401, "Unauthorized", {}, body)
        with mock.patch("urllib.request.urlopen", side_effect=error), mock.patch.object(
            image_generate, "safe_log"
        ):
            with self.assertRaisesRegex(RuntimeError, "CLOUDFLARE_AUTH_ERROR"):
                image_generate.generate_cloudflare("test prompt", "account", "token")

    def test_cloudflare_handles_exhausted_free_allocation(self):
        body = io.BytesIO(
            json.dumps(
                {
                    "success": False,
                    "errors": [{"code": 3036, "message": "Daily free allocation exhausted"}],
                }
            ).encode("utf-8")
        )
        error = urllib.error.HTTPError("https://api.cloudflare.com", 429, "Too Many Requests", {}, body)
        with mock.patch("urllib.request.urlopen", side_effect=error), mock.patch.object(
            image_generate, "safe_log"
        ):
            with self.assertRaisesRegex(RuntimeError, r"BLOCKED_COST\[3036\]"):
                image_generate.generate_cloudflare("test prompt", "account", "token")

    def test_cloudflare_retries_network_failure_then_stops(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("offline")
        ) as urlopen, mock.patch.object(image_generate.time, "sleep"), mock.patch.object(
            image_generate, "safe_log"
        ):
            with self.assertRaisesRegex(RuntimeError, "CLOUDFLARE_NETWORK_ERROR"):
                image_generate.generate_cloudflare("test prompt", "account", "token")
        self.assertEqual(urlopen.call_count, 3)

    def test_cloudflare_retries_transient_8007(self):
        def transient_error():
            body = io.BytesIO(json.dumps({
                "success": False,
                "errors": [{"code": 8007, "message": "Upstream execution failed"}],
            }).encode("utf-8"))
            return urllib.error.HTTPError(
                "https://api.cloudflare.com", 500, "Internal Server Error", {}, body
            )

        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), "brown").save(buffer, format="JPEG")
        payload = {
            "success": True,
            "result": {"image": base64.b64encode(buffer.getvalue()).decode("ascii")},
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[transient_error(), transient_error(), response],
        ) as urlopen, mock.patch.object(image_generate.time, "sleep"), mock.patch.object(
            image_generate, "safe_log"
        ):
            image_bytes, _ = image_generate.generate_cloudflare("test prompt", "account", "token")
        self.assertEqual(urlopen.call_count, 3)
        self.assertTrue(image_bytes.startswith(b"\xff\xd8\xff"))

    def test_cloudflare_output_flows_into_background_and_thumbnail(self):
        settings = dict(image_generate.SETTINGS)
        try:
            image_generate.SETTINGS.update(
                {
                    "image_provider": "cloudflare_workers_ai",
                    "image_model": "@cf/black-forest-labs/flux-1-schnell",
                    "image_steps": 4,
                    "image_cost_mode": "free_allocation_only",
                    "image_fallback_provider": "pollinations",
                }
            )
            buffer = io.BytesIO()
            image = Image.new("RGB", (512, 512))
            for x in range(512):
                for y in range(512):
                    image.putpixel((x, y), ((x * 3) % 256, (y * 5) % 256, ((x + y) * 7) % 256))
            image.save(buffer, format="JPEG", quality=90)
            with tempfile.TemporaryDirectory() as directory:
                story = Path(directory)
                (story / "08_image_prompt_en.txt").write_text(
                    "ORIGINAL DYNAMIC PROMPT", encoding="utf-8"
                )
                (story / "15_metadata.json").write_text("{}", encoding="utf-8")
                with mock.patch.object(
                    image_generate,
                    "load_secrets",
                    return_value={
                        "CLOUDFLARE_ACCOUNT_ID": "account-id",
                        "CLOUDFLARE_API_TOKEN": "token",
                    },
                ), mock.patch.object(
                    image_generate,
                    "generate_cloudflare",
                    return_value=(buffer.getvalue(), {"success": True}),
                ) as cloudflare, mock.patch.object(
                    image_generate, "historical_accuracy_gate", return_value={"status": "PASS"}
                ):
                    image_generate.generate(story, seed=123)
                cloudflare.assert_called_once_with(
                    "ORIGINAL DYNAMIC PROMPT", "account-id", "token"
                )
                with Image.open(story / "10_background.png") as background:
                    self.assertEqual(background.size, (1920, 1080))
                with Image.open(story / "11_thumbnail_base.jpg") as thumbnail:
                    self.assertEqual(thumbnail.size, (1280, 720))
                source = (story / "09_image_source.md").read_text(encoding="utf-8")
                self.assertIn("cloudflare_workers_ai", source)
                self.assertIn("@cf/black-forest-labs/flux-1-schnell", source)
        finally:
            image_generate.SETTINGS.clear()
            image_generate.SETTINGS.update(settings)

    def test_cloudflare_low_quality_output_uses_pollinations_fallback(self):
        settings = dict(image_generate.SETTINGS)
        try:
            image_generate.SETTINGS.update(
                {
                    "image_provider": "cloudflare_workers_ai",
                    "image_model": "@cf/black-forest-labs/flux-1-schnell",
                    "image_cost_mode": "free_allocation_only",
                    "image_fallback_provider": "pollinations",
                    "pollinations_image_model": "microsoft/mai-image-2.6",
                    "pollinations_cost_mode": "quest_pollen_only",
                }
            )
            bad_buffer = io.BytesIO()
            Image.new("RGB", (512, 512), "gray").save(bad_buffer, format="JPEG")
            good_buffer = io.BytesIO()
            good_image = Image.new("RGB", (512, 512))
            for x in range(512):
                for y in range(512):
                    good_image.putpixel((x, y), ((x * 3) % 256, (y * 5) % 256, ((x + y) * 7) % 256))
            good_image.save(good_buffer, format="JPEG", quality=90)

            with tempfile.TemporaryDirectory() as directory:
                story = Path(directory)
                (story / "08_image_prompt_en.txt").write_text("ORIGINAL DYNAMIC PROMPT", encoding="utf-8")
                (story / "15_metadata.json").write_text("{}", encoding="utf-8")
                with mock.patch.object(
                    image_generate,
                    "load_secrets",
                    return_value={
                        "CLOUDFLARE_ACCOUNT_ID": "account-id",
                        "CLOUDFLARE_API_TOKEN": "token",
                        "POLLINATIONS_API_KEY": "pollinations-key",
                    },
                ), mock.patch.object(
                    image_generate,
                    "generate_cloudflare",
                    return_value=(bad_buffer.getvalue(), {"success": True}),
                ) as cloudflare, mock.patch.object(
                    image_generate,
                    "generate_pollinations",
                    return_value=(good_buffer.getvalue(), {"name": "microsoft/mai-image-2.6"}),
                ) as pollinations, mock.patch.object(
                    image_generate, "historical_accuracy_gate", return_value={"status": "PASS"}
                ):
                    image_generate.generate(story, seed=123)

                cloudflare.assert_called_once()
                pollinations.assert_called_once()
                source = (story / "09_image_source.md").read_text(encoding="utf-8")
                self.assertIn("pollinations", source)
                self.assertTrue((story / "10_background.png").exists())
        finally:
            image_generate.SETTINGS.clear()
            image_generate.SETTINGS.update(settings)

    def test_bytez_requires_explicit_cost_mode_and_no_fallback(self):
        settings = dict(image_generate.SETTINGS)
        try:
            image_generate.SETTINGS["bytez_cost_mode"] = "free_or_manually_funded_credits_only"
            image_generate.SETTINGS["image_fallback_provider"] = None
            image_generate.SETTINGS["bytez_image_model"] = "fal/FLUX.2-dev-Turbo"
            with mock.patch("urllib.request.urlopen") as urlopen:
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(
                    {"error": None, "output": "data:image/png;base64,aW1hZ2U="}
                ).encode("utf-8")
                urlopen.return_value = response
                image_bytes, _ = image_generate.generate_bytez("test", "test-key")
            self.assertEqual(image_bytes, b"image")
        finally:
            image_generate.SETTINGS.clear()
            image_generate.SETTINGS.update(settings)

    def test_bytez_rejects_unapproved_cost_mode_before_network(self):
        settings = dict(image_generate.SETTINGS)
        try:
            image_generate.SETTINGS["bytez_cost_mode"] = "unlimited"
            with mock.patch("urllib.request.urlopen") as urlopen:
                with self.assertRaisesRegex(RuntimeError, "BLOCKED_COST"):
                    image_generate.generate_bytez("test", "test-key")
                urlopen.assert_not_called()
        finally:
            image_generate.SETTINGS.clear()
            image_generate.SETTINGS.update(settings)

    def test_accepts_image_only_quest_key_for_non_paid_model(self):
        model = {"name": "microsoft/mai-image-2.6", "paid_only": False}
        key = {
            "valid": True,
            "questPollenOnly": True,
            "permissions": {"models": ["image"], "account": []},
        }
        settings = dict(image_generate.SETTINGS)
        try:
            image_generate.SETTINGS["pollinations_cost_mode"] = "quest_pollen_only"
            with mock.patch.object(image_generate, "model_record", return_value=model), mock.patch.object(
                image_generate, "key_record", return_value=key
            ):
                self.assertEqual(
                    image_generate.enforce_free_model("microsoft/mai-image-2.6", "test-key"),
                    model,
                )
        finally:
            image_generate.SETTINGS.clear()
            image_generate.SETTINGS.update(settings)

    def test_rejects_paid_only_model(self):
        with mock.patch.object(
            image_generate,
            "model_record",
            return_value={"name": "paid-model", "paid_only": True},
        ):
            with self.assertRaisesRegex(RuntimeError, "BLOCKED_COST"):
                image_generate.enforce_free_model("paid-model", "test-key")

    def test_rejects_key_that_can_spend_paid_pollen(self):
        model = {"name": "microsoft/mai-image-2.6", "paid_only": False}
        key = {
            "valid": True,
            "questPollenOnly": False,
            "permissions": {"models": ["image"], "account": []},
        }
        with mock.patch.object(image_generate, "model_record", return_value=model), mock.patch.object(
            image_generate, "key_record", return_value=key
        ):
            with self.assertRaisesRegex(RuntimeError, "BLOCKED_COST"):
                image_generate.enforce_free_model("microsoft/mai-image-2.6", "test-key")


if __name__ == "__main__":
    unittest.main()
