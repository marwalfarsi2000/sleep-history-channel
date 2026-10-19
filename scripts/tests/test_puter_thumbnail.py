"""Puter.js / GPT Image 2.5 Sunburst final thumbnail: routing, free-only guard, no overlay, no paid fallback."""
import inspect
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import image_generate  # noqa: E402
import puter_thumbnail as puter  # noqa: E402
import sunburst_thumbnail as shared  # noqa: E402

TEXT = "داخل ورشة سمرقند"
TOKEN = "puter-test-token-0123456789-not-real"


def noisy(size=(1920, 1080)) -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise(size, 80).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def story(tmp_path, monkeypatch):
    monkeypatch.setenv("PUTER_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("PUTER_FREE_ONLY_VERIFIED", "1")
    monkeypatch.setattr(puter.time, "sleep", lambda s: None)
    folder = tmp_path / "story"
    folder.mkdir()
    Image.effect_noise((1920, 1080), 60).convert("RGB").save(folder / "10_background.png")
    return folder


def adapter(results):
    """Fake Node adapter: each call consumes one result ('image' bytes, or a dict answer); the last repeats."""
    calls = []

    def run(args, timeout):
        calls.append(args)
        item = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(item, (bytes, bytearray)):
            Path(args[2]).write_bytes(item)
            return {"ok": True, "bytes": len(item)}
        return item
    run.calls = calls
    return run


def test_missing_auth_stops_without_calling_the_adapter(story, monkeypatch):
    monkeypatch.delenv("PUTER_AUTH_TOKEN")
    fake = adapter([noisy()])
    monkeypatch.setattr(puter, "run_adapter", fake)
    with pytest.raises(puter.PuterError) as info:
        puter.generate(story, TEXT)
    assert info.value.code == "PUTER_AUTH_MISSING" and not fake.calls and not puter.enabled()


def test_invalid_auth_is_a_stop_error_not_retried(story, monkeypatch):
    fake = adapter([{"ok": False, "code": "PUTER_AUTH_FAILED", "detail": "401"}])
    monkeypatch.setattr(puter, "run_adapter", fake)
    with pytest.raises(puter.PuterError) as info:
        puter.generate(story, TEXT)
    assert info.value.code == "PUTER_AUTH_FAILED" and len(fake.calls) == 1


def test_successful_mocked_generation_keeps_native_text_and_exact_settings(story, monkeypatch):
    fake = adapter([noisy()])
    monkeypatch.setattr(puter, "run_adapter", fake)
    record = puter.generate(story, TEXT)
    assert record["thumbnail_generation_provider"] == "puter" and record["thumbnail_model"] == "openai/gpt-image-2.5-sunburst"
    assert record["thumbnail_text_render_method"] == "native_in_generated_image"
    assert record["thumbnail_size"] == "1920x1080" and record["thumbnail_generation_quality"] == "medium"
    assert record["thumbnail_attempts"] == 1 and len(fake.calls) == 1
    with Image.open(story / "11_thumbnail_base.jpg") as image:
        assert image.size == (1920, 1080) and image.format == "JPEG"
    assert (story / "11_thumbnail_base.jpg").stat().st_size <= 2_000_000
    assert f"«{TEXT}»" in shared.build_prompt(TEXT)


def test_invalid_image_is_retried_then_succeeds_and_stops_at_first_success(story, monkeypatch):
    fake = adapter([b"<html>error</html>", noisy((1280, 720)), noisy()])
    monkeypatch.setattr(puter, "run_adapter", fake)
    record = puter.generate(story, TEXT)
    assert record["thumbnail_attempts"] == 3 and len(fake.calls) == 3


def test_never_more_than_four_attempts(story, monkeypatch):
    fake = adapter([{"ok": False, "code": "PUTER_REQUEST_FAILED", "detail": "boom"}])
    monkeypatch.setattr(puter, "run_adapter", fake)
    with pytest.raises(puter.PuterError):
        puter.generate(story, TEXT, max_attempts=10)
    assert len(fake.calls) == 4


def test_free_limit_stops_immediately(story, monkeypatch):
    fake = adapter([{"ok": False, "code": "PUTER_FREE_ALLOWANCE_EXHAUSTED", "detail": "402"}])
    monkeypatch.setattr(puter, "run_adapter", fake)
    with pytest.raises(puter.PuterError) as info:
        puter.generate(story, TEXT)
    assert info.value.code == "PUTER_FREE_ALLOWANCE_EXHAUSTED" and len(fake.calls) == 1


def test_routing_uses_puter_and_never_draws_text_afterwards(story, monkeypatch):
    monkeypatch.setattr(puter, "run_adapter", adapter([noisy()]))
    called = []
    monkeypatch.setattr(image_generate, "render_thumbnail", lambda *a, **k: called.append(a))
    record, status = image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT)
    assert status == "passed" and record["thumbnail_generation_provider"] == "puter" and not called


def test_puter_failure_falls_back_to_the_existing_free_path_only(story, monkeypatch):
    monkeypatch.setattr(puter, "run_adapter", adapter([{"ok": False, "code": "PUTER_FREE_ALLOWANCE_EXHAUSTED", "detail": ""}]))
    called = []

    def fake_render(base, path, text):
        called.append(text)
        base.resize((1280, 720)).save(path, format="JPEG")
    monkeypatch.setattr(image_generate, "render_thumbnail", fake_render)
    record, status = image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT)
    assert called == [TEXT] and status == "fallback_overlay"
    assert record["thumbnail_generation_provider"] == "local_pillow_fallback" and record["thumbnail_puter_status"] == "PUTER_FREE_ALLOWANCE_EXHAUSTED"


def test_not_free_only_verified_never_reaches_puter(story, monkeypatch):
    monkeypatch.delenv("PUTER_FREE_ONLY_VERIFIED")
    fake = adapter([noisy()])
    monkeypatch.setattr(puter, "run_adapter", fake)
    monkeypatch.setattr(image_generate, "render_thumbnail",
                        lambda base, path, text: base.resize((1280, 720)).save(path, format="JPEG"))
    record, _ = image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT)
    assert not fake.calls and record["thumbnail_puter_status"] == "PUTER_DISABLED"


def test_no_paid_provider_is_referenced():
    for name in ("puter_thumbnail.py", "generate_image_puter.mjs"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8").lower()
        for banned in ("api.openai.com", "ai-gateway.vercel.sh", "stripe", "top-up", "topup"):
            assert banned not in text, (name, banned)
    source = inspect.getsource(image_generate.build_final_thumbnail)
    assert "vercel" not in source.lower() and "sunburst_thumbnail" not in source


def test_token_is_never_leaked(story, monkeypatch, tmp_path, capsys):
    secret_error = {"ok": False, "code": "PUTER_REQUEST_FAILED", "detail": f"bad token {TOKEN}"}
    monkeypatch.setattr(puter, "run_adapter", adapter([secret_error]))
    with pytest.raises(puter.PuterError) as info:
        puter.generate(story, TEXT, max_attempts=1)
    assert TOKEN not in str(info.value)
    log = (story / "logs" / "thumbnail_attempts.json").read_text(encoding="utf-8")
    assert TOKEN not in log and TOKEN not in capsys.readouterr().out
    assert puter.redact(f"x {TOKEN} y") == "x *** y"


def test_adapter_refuses_without_free_only_flag_or_token():
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    out = subprocess.run(["node", str(ROOT / "scripts" / "generate_image_puter.mjs"), "a", "b", "c"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert json.loads(out.stdout)["code"] == "PUTER_AUTH_MISSING"
    out = subprocess.run(["node", str(ROOT / "scripts" / "generate_image_puter.mjs"), "a", "b", "c"],
                         capture_output=True, text=True, env={**env, "PUTER_AUTH_TOKEN": "x"}, timeout=30)
    assert json.loads(out.stdout)["code"] == "PUTER_FREE_ONLY_NOT_GUARANTEED"


def test_workflows_pass_token_via_env_only_and_install_node_deps():
    for name in ("production.yml", "review.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "PUTER_AUTH_TOKEN: ${{ secrets.PUTER_AUTH_TOKEN }}" in text and "npm ci --ignore-scripts" in text
        assert not re.search(r"echo[^\n]*PUTER_AUTH_TOKEN|printenv|set -x", text)
    assert "AI_GATEWAY_API_KEY" not in (ROOT / ".github" / "workflows" / "production.yml").read_text(encoding="utf-8")


def test_video_background_stays_text_free(story, monkeypatch):
    before = (story / "10_background.png").read_bytes()
    monkeypatch.setattr(puter, "run_adapter", adapter([noisy()]))
    image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT)
    assert (story / "10_background.png").read_bytes() == before


def test_adapter_source_guards_allowance_and_never_retries_402():
    text = (ROOT / "scripts" / "generate_image_puter.mjs").read_text(encoding="utf-8")
    assert "getMonthlyUsage" in text and "PUTER_MIN_ALLOWANCE" in text and "PUTER_FREE_ALLOWANCE_EXHAUSTED" in text
    assert "status === 402" in text and "insufficient_funds" in text
    assert "allowance_before" in text and "allowance_after" in text
