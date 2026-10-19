"""Canva (via n8n) final thumbnail: routing, validation, no overlay, secret handling."""
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import canva_thumbnail as canva  # noqa: E402
import image_generate  # noqa: E402

TEXT = "داخل ورشة سمرقند"
URL = "https://n8n.example.test/webhook/canva-thumbnail-0123456789abcdef-not-real"


def png(size=(1280, 720)) -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise(size, 80).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


class Net:
    def __init__(self, *answers):
        self.answers = list(answers) or [(200, png())]
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append((url, json.loads(payload)))
        return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]


@pytest.fixture
def story(tmp_path, monkeypatch):
    monkeypatch.setenv(canva.URL_ENV, URL)
    monkeypatch.setattr(canva.time, "sleep", lambda s: None)
    folder = tmp_path / "story"
    folder.mkdir()
    Image.effect_noise((1920, 1080), 60).convert("RGB").save(folder / "10_background.png")
    return folder


def test_success_sends_background_and_text_and_writes_1280x720_jpeg(story, monkeypatch):
    net = Net()
    monkeypatch.setattr(canva, "transport", net)
    record = canva.generate(story, TEXT)
    url, body = net.calls[0]
    assert url == URL and body["hook_text"] == TEXT and len(body["background_b64"]) > 1000
    assert record["thumbnail_generation_provider"] == "canva_via_n8n" and record["thumbnail_size"] == "1280x720"
    assert record["thumbnail_text_render_method"] == "canva_autofill_template"
    with Image.open(story / "11_thumbnail_base.jpg") as image:
        assert image.size == (1280, 720) and image.format == "JPEG"
    assert (story / "11_thumbnail_base.jpg").stat().st_size <= 2_000_000


def test_missing_webhook_stops_without_a_call(story, monkeypatch):
    monkeypatch.delenv(canva.URL_ENV)
    net = Net()
    monkeypatch.setattr(canva, "transport", net)
    with pytest.raises(canva.CanvaError) as info:
        canva.generate(story, TEXT)
    assert info.value.code == "CANVA_WEBHOOK_MISSING" and not net.calls and not canva.enabled()


@pytest.mark.parametrize("answer", [(200, b"<html>login</html>"), (200, b""), (200, png((640, 360)))])
def test_invalid_exports_are_rejected_and_retried_at_most_twice(story, monkeypatch, answer):
    net = Net(answer)
    monkeypatch.setattr(canva, "transport", net)
    with pytest.raises(canva.CanvaError):
        canva.generate(story, TEXT, max_attempts=10)
    assert len(net.calls) == 2 and not (story / "11_thumbnail_base.jpg").exists()


def test_auth_failure_stops_immediately(story, monkeypatch):
    net = Net((401, b"unauthorized"))
    monkeypatch.setattr(canva, "transport", net)
    with pytest.raises(canva.CanvaError) as info:
        canva.generate(story, TEXT)
    assert info.value.code == "CANVA_AUTH_FAILED" and len(net.calls) == 1


def test_first_valid_export_wins(story, monkeypatch):
    net = Net((500, b"x"), (200, png()))
    monkeypatch.setattr(canva, "transport", net)
    assert canva.generate(story, TEXT)["thumbnail_attempts"] == 2


def test_routing_prefers_canva_and_never_overlays_text(story, monkeypatch):
    monkeypatch.setattr(canva, "transport", Net())
    drawn = []
    monkeypatch.setattr(image_generate, "render_thumbnail", lambda *a, **k: drawn.append(a))
    record, status = image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT)
    assert status == "passed" and record["thumbnail_generation_provider"] == "canva_via_n8n" and not drawn


def test_canva_failure_falls_back_to_the_existing_free_path(story, monkeypatch):
    monkeypatch.setattr(canva, "transport", Net((500, b"x")))
    drawn = []

    def fake_render(base, path, text):
        drawn.append(text)
        base.resize((1280, 720)).save(path, format="JPEG")
    monkeypatch.setattr(image_generate, "render_thumbnail", fake_render)
    record, status = image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT)
    assert drawn == [TEXT] and status == "fallback_overlay" and record["thumbnail_puter_status"].startswith("CANVA_FAILED")


def test_background_for_the_video_stays_text_free_and_untouched(story, monkeypatch):
    before = (story / "10_background.png").read_bytes()
    monkeypatch.setattr(canva, "transport", Net())
    image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT)
    assert (story / "10_background.png").read_bytes() == before


def test_webhook_url_is_never_leaked(story, monkeypatch, capsys):
    monkeypatch.setattr(canva, "transport", Net((500, f"boom {URL}".encode())))
    with pytest.raises(canva.CanvaError) as info:
        canva.generate(story, TEXT, max_attempts=1)
    assert URL not in str(info.value)
    assert URL not in (story / "logs" / "thumbnail_attempts.json").read_text(encoding="utf-8")
    assert URL not in capsys.readouterr().out
    assert canva.redact(f"x {URL} y") == "x *** y"


def test_module_draws_no_text_and_uses_no_canva_image_generation():
    source = (ROOT / "scripts" / "canva_thumbnail.py").read_text(encoding="utf-8")
    assert "ImageDraw" not in source and "ffmpeg" not in source and ".text(" not in source
    assert "generateImage" not in source and "/images" not in source


def test_workflows_pass_the_webhook_secret_via_env_only():
    for name in ("production.yml", "review.yml", "canva-live-test.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "CANVA_THUMBNAIL_WEBHOOK_URL: ${{ secrets.CANVA_THUMBNAIL_WEBHOOK_URL }}" in text
        assert "printenv" not in text and "set -x" not in text
