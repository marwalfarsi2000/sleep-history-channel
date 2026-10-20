from __future__ import annotations

import json
import base64
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_generate
from PIL import Image


class ImageCostGuardTests(unittest.TestCase):
    def test_historical_accuracy_rejection_generates_a_new_candidate(self):
        rejected = RuntimeError(
            'IMAGE_ACCURACY_GATE_FAILED: {"status": "FAIL", "issues": ["modern wires"]}'
        )
        providers = []

        def attempt(*_args):
            providers.append(image_generate.SETTINGS["image_provider"])
            if len(providers) == 1:
                raise rejected

        with tempfile.TemporaryDirectory() as directory:
            story = Path(directory)
            prompt = story / "08_image_prompt_en.txt"
            prompt.write_text("A historical market", encoding="utf-8")
            with mock.patch.object(image_generate, "generate", side_effect=attempt) as generate, \
                 mock.patch.dict(image_generate.SETTINGS, {
                     "image_accuracy_retry_attempts": 4,
                     "image_provider": "cloudflare_workers_ai",
                     "image_fallback_provider": "pollinations",
                 }):
                image_generate.generate_with_accuracy_retries(story, seed=100)
                self.assertEqual(image_generate.SETTINGS["image_provider"], "cloudflare_workers_ai")
            self.assertIn("no electricity", prompt.read_text(encoding="utf-8"))
        self.assertEqual(providers, ["cloudflare_workers_ai", "cloudflare_workers_ai"])
        self.assertEqual(generate.call_args_list[0].args[1], 100)
        self.assertEqual(generate.call_args_list[1].args[1], 101)

    def test_non_accuracy_failure_is_not_retried(self):
        failure = RuntimeError("BLOCKED_COST: quota exhausted")
        with mock.patch.object(
            image_generate, "generate", side_effect=failure
        ) as generate:
            with self.assertRaisesRegex(RuntimeError, "BLOCKED_COST"):
                image_generate.generate_with_accuracy_retries(Path("story"), seed=100)
        generate.assert_called_once()

    def test_cloudflare_sends_guarded_prompt_and_supported_fields_only(self):
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
                    image_generate.base_generation_prompt("ORIGINAL DYNAMIC PROMPT"),
                    "account-id",
                    "token",
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


REJECTED = 'IMAGE_ACCURACY_GATE_FAILED: {"status": "FAIL", "issues": ["modern wires"]}'
CLOUDFLARE_SETTINGS = {
    "image_accuracy_retry_attempts": 4,
    "image_provider": "cloudflare_workers_ai",
    "image_fallback_provider": "pollinations",
    "image_model": "@cf/black-forest-labs/flux-1-schnell",
    "pollinations_image_model": "microsoft/mai-image-2.6",
    "image_cost_mode": "free_allocation_only",
    "pollinations_cost_mode": "quest_pollen_only",
}


class AccuracyRetryRoutingTests(unittest.TestCase):
    def make_story(self, directory: str) -> Path:
        story = Path(directory)
        (story / "08_image_prompt_en.txt").write_text("A paper workshop in Samarkand", encoding="utf-8")
        (story / "15_metadata.json").write_text(json.dumps({"thumbnail_text": ""}), encoding="utf-8")
        return story

    def run_retries(self, story, *, cloudflare, pollinations=None, gate=None):
        """Run the real retry loop and generate() with every network call mocked."""
        gate = gate or (lambda *_a: {"status": "PASS"})
        pollinations = pollinations or mock.Mock(side_effect=AssertionError("Pollinations must not be called"))
        image = Image.new("RGB", (64, 64), "brown")
        with mock.patch.dict(image_generate.SETTINGS, CLOUDFLARE_SETTINGS), \
             mock.patch.object(image_generate, "load_secrets", return_value={
                 "CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t", "POLLINATIONS_API_KEY": "k",
             }), \
             mock.patch.object(image_generate, "generate_cloudflare", cloudflare), \
             mock.patch.object(image_generate, "generate_pollinations", pollinations), \
             mock.patch.object(image_generate, "prepare_image", return_value=image), \
             mock.patch.object(image_generate, "historical_accuracy_gate", side_effect=gate):
            image_generate.generate_with_accuracy_retries(story, seed=1)
        return pollinations

    def test_pass_first_try_uses_one_cloudflare_call_and_no_pollinations(self):
        cloudflare = mock.Mock(return_value=(b"x", {}))
        with tempfile.TemporaryDirectory() as directory:
            pollinations = self.run_retries(self.make_story(directory), cloudflare=cloudflare)
        self.assertEqual(cloudflare.call_count, 1)
        pollinations.assert_not_called()

    def test_quality_rejections_stay_on_cloudflare_until_pass(self):
        cloudflare = mock.Mock(return_value=(b"x", {}))
        outcomes = [RuntimeError(REJECTED), RuntimeError(REJECTED), {"status": "PASS"}]

        def gate(*_a):
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with tempfile.TemporaryDirectory() as directory:
            story = self.make_story(directory)
            pollinations = self.run_retries(story, cloudflare=cloudflare, gate=gate)
            prompts = [call.args[0] for call in cloudflare.call_args_list]
        self.assertEqual(cloudflare.call_count, 3)
        pollinations.assert_not_called()
        self.assertIn("NO TEXT ANYWHERE", prompts[0])
        self.assertIn("STRICT HISTORICAL EXCLUSION", prompts[0])
        self.assertIn("TEXT-FREE SURFACES", prompts[1])
        self.assertIn("TEXT-FREE SURFACES", prompts[2])

    def test_repeated_text_rejection_escalates_without_rewriting_other_constraints(self):
        gate = {
            "status": "FAIL",
            "historically_plausible": True,
            "no_modern_objects": True,
            "no_text_or_watermark": False,
            "topic_match": True,
            "issues": ["calligraphy on wall"],
        }
        first = image_generate.tighten_prompt("A historical caravanserai", gate)
        second = image_generate.tighten_prompt(first, gate)
        self.assertIn("NO TEXT ANYWHERE", first)
        self.assertIn("TEXT-FREE SURFACES", first)
        self.assertNotIn("COMPOSITION SAFETY", first)
        self.assertIn("COMPOSITION SAFETY", second)
        self.assertEqual(second.count("NO TEXT ANYWHERE"), 1)
        self.assertLessEqual(len(second.strip()), image_generate.PROMPT_LIMIT)

    def test_required_image_guard_never_silently_drops_for_prompt_length(self):
        too_long = "x" * image_generate.PROMPT_LIMIT
        with self.assertRaisesRegex(RuntimeError, "IMAGE_PROMPT_TOO_LONG_FOR_REQUIRED_GUARD"):
            image_generate.base_generation_prompt(too_long)

    def test_quality_rejection_is_not_a_provider_failure(self):
        cloudflare = mock.Mock(return_value=(b"x", {}))

        def gate(*_a):
            raise RuntimeError(REJECTED)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "IMAGE_ACCURACY_GATE_FAILED"):
                self.run_retries(self.make_story(directory), cloudflare=cloudflare, gate=gate)
        self.assertEqual(cloudflare.call_count, 4)  # image_accuracy_retry_attempts, all on Cloudflare

    def test_max_attempts_is_four_from_settings(self):
        import json as js
        settings = js.loads((Path(__file__).resolve().parents[2] / "config" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["image_accuracy_retry_attempts"], 4)

    def test_genuine_cloudflare_provider_failure_may_use_pollinations(self):
        cloudflare = mock.Mock(side_effect=RuntimeError("CLOUDFLARE_NETWORK_ERROR: timeout"))
        pollinations = mock.Mock(return_value=(b"y", {"free": True}))
        with tempfile.TemporaryDirectory() as directory:
            self.run_retries(self.make_story(directory), cloudflare=cloudflare, pollinations=pollinations)
        self.assertEqual(cloudflare.call_count, 1)
        self.assertEqual(pollinations.call_count, 1)

    def test_pollinations_402_fails_closed_without_retry(self):
        http_402 = urllib.error.HTTPError("https://gen.pollinations.ai", 402, "Payment Required", {}, None)
        with mock.patch.dict(image_generate.SETTINGS, CLOUDFLARE_SETTINGS), \
             mock.patch.object(image_generate, "enforce_free_model", return_value={}), \
             mock.patch("urllib.request.urlopen", side_effect=http_402) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "BLOCKED_COST_OR_FREE_LIMIT: Pollinations free/Quest Pollen capacity unavailable"):
                image_generate.generate_pollinations("prompt", "key", 1)
        self.assertEqual(urlopen.call_count, 1)

    def test_cloudflare_exhausted_then_pollinations_402_is_free_capacity_exhausted(self):
        cloudflare = mock.Mock(side_effect=RuntimeError("BLOCKED_COST[3036]: Workers AI free allocation exhausted"))
        http_402 = urllib.error.HTTPError("https://gen.pollinations.ai", 402, "Payment Required", {}, None)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(image_generate, "enforce_free_model", return_value={}), \
             mock.patch("urllib.request.urlopen", side_effect=http_402) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "IMAGE_FREE_CAPACITY_EXHAUSTED: BLOCKED_COST_OR_FREE_LIMIT"):
                self.run_retries(self.make_story(directory), cloudflare=cloudflare, pollinations=image_generate.generate_pollinations)
        self.assertEqual(cloudflare.call_count, 1)
        self.assertEqual(urlopen.call_count, 1)  # no repeated attempts after 402

    def test_historical_accuracy_gate_source_unchanged(self):
        import inspect
        source = inspect.getsource(image_generate.historical_accuracy_gate)
        self.assertIn("IMAGE_ACCURACY_GATE_FAILED", source)
        self.assertIn("BLOCKED_COST: unapproved image accuracy model", source)


if __name__ == "__main__":
    unittest.main()
