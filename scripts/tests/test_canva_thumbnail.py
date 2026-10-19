"""Production thumbnail path: Canva only (Lucid Origin background, template autofill, export). No fallback, no overlay."""
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import canva_thumbnail as canva  # noqa: E402
import failure_registry as registry  # noqa: E402
import image_generate  # noqa: E402

TEXT = "داخل ورشة سمرقند"
SCENE = "16:9 YouTube thumbnail background, a calm Samarkand workshop at golden hour, photorealistic scene"
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


def err(code, status=502):
    return status, json.dumps({"error": code}).encode()


@pytest.fixture
def story(tmp_path, monkeypatch):
    monkeypatch.setenv(canva.URL_ENV, URL)
    monkeypatch.setattr(canva.time, "sleep", lambda s: None)
    folder = tmp_path / "story"
    folder.mkdir()
    Image.effect_noise((1920, 1080), 60).convert("RGB").save(folder / "10_background.png")
    (folder / "08_image_prompt_en.txt").write_text(SCENE, encoding="utf-8")
    return folder


@pytest.fixture
def forbidden(monkeypatch):
    """Anything that could write the thumbnail another way must never be called on the production path."""
    calls = []
    for name in ("render_thumbnail", "generate_cloudflare", "generate_pollinations", "generate_bytez"):
        if hasattr(image_generate, name):
            monkeypatch.setattr(image_generate, name, lambda *a, _n=name, **k: calls.append(_n))
    import puter_thumbnail
    monkeypatch.setattr(puter_thumbnail, "generate", lambda *a, **k: calls.append("puter"))
    monkeypatch.setattr(puter_thumbnail, "run_adapter", lambda *a, **k: calls.append("puter_adapter"))
    return calls


def final(story):
    return image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT, SCENE)


def test_success_passes_prompt_and_hook_text_and_writes_1280x720_jpeg(story, monkeypatch, forbidden):
    net = Net()
    monkeypatch.setattr(canva, "transport", net)
    record, status = final(story)
    url, body = net.calls[0]
    assert url == URL and body["hook_text"] == TEXT and set(body) == {"hook_text", "background_prompt"}
    assert "no text" in body["background_prompt"].lower() and SCENE in body["background_prompt"]
    assert status == "passed" and record["thumbnail_generation_provider"] == "canva_via_n8n"
    assert record["thumbnail_background_model"] == "lucid_origin" and record["thumbnail_text_render_method"] == "canva_autofill_template"
    with Image.open(story / "11_thumbnail_base.jpg") as image:
        assert image.size == (1280, 720) and image.format == "JPEG"
    assert (story / "11_thumbnail_base.jpg").stat().st_size <= 2_000_000
    assert forbidden == []


@pytest.mark.parametrize("answer, code", [
    (err("CANVA_BACKGROUND_GENERATION_FAILED"), "CANVA_BACKGROUND_GENERATION_FAILED"),
    (err("CANVA_AUTOFILL_FAILED"), "CANVA_AUTOFILL_FAILED"),
    (err("CANVA_EXPORT_FAILED"), "CANVA_EXPORT_FAILED"),
    (err("CANVA_TEMPLATE_INVALID", 409), "CANVA_TEMPLATE_INVALID"),
    ((500, b"<html>boom</html>"), "CANVA_THUMBNAIL_FAILED"),
    ((401, b"no"), "CANVA_AUTH_MISSING"),
    ((200, b"<html>login</html>"), "CANVA_EXPORT_FAILED"),
    ((200, png((640, 360))), "CANVA_EXPORT_FAILED"),
    ((200, b""), "CANVA_EXPORT_FAILED"),
])
def test_canva_failure_stops_with_a_clear_code_and_no_other_provider(story, monkeypatch, forbidden, answer, code):
    net = Net(answer)
    monkeypatch.setattr(canva, "transport", net)
    with pytest.raises(RuntimeError) as info:
        final(story)
    assert str(info.value).startswith(f"{code}:")
    assert registry.extract_error_code(info.value) == code
    assert len(net.calls) == 1 and forbidden == [] and not (story / "11_thumbnail_base.jpg").exists()


def test_missing_webhook_is_canva_auth_missing_and_makes_no_thumbnail(story, monkeypatch, forbidden):
    monkeypatch.delenv(canva.URL_ENV)
    net = Net()
    monkeypatch.setattr(canva, "transport", net)
    with pytest.raises(RuntimeError) as info:
        final(story)
    assert str(info.value).startswith("CANVA_AUTH_MISSING") and not net.calls and forbidden == []


def test_unreachable_workflow_is_retried_once_then_fails_without_fallback(story, monkeypatch, forbidden):
    calls = []

    def down(url, payload, timeout):
        calls.append(1)
        raise canva.CanvaError("CANVA_UNREACHABLE", "URLError")
    monkeypatch.setattr(canva, "transport", down)
    with pytest.raises(RuntimeError) as info:
        final(story)
    assert str(info.value).startswith("CANVA_THUMBNAIL_FAILED") and len(calls) == 2 and forbidden == []


def test_hook_text_must_be_2_to_4_arabic_words(story, monkeypatch):
    monkeypatch.setattr(canva, "transport", Net())
    for bad in ("كلمة", "هذه عبارة أطول من اللازم لصورة مصغرة واضحة جدا"):
        with pytest.raises(RuntimeError):
            image_generate.build_final_thumbnail(story, story / "10_background.png", bad, SCENE)


def test_background_prompt_falls_back_to_the_scene_prompt_and_is_text_free(story, monkeypatch):
    net = Net()
    monkeypatch.setattr(canva, "transport", net)
    image_generate.build_final_thumbnail(story, story / "10_background.png", TEXT, "")
    sent = net.calls[0][1]["background_prompt"]
    assert SCENE in sent and "Absolutely no text" in sent and len(sent) <= 1900


def test_video_background_stays_untouched_and_text_free(story, monkeypatch):
    before = (story / "10_background.png").read_bytes()
    monkeypatch.setattr(canva, "transport", Net())
    final(story)
    assert (story / "10_background.png").read_bytes() == before


def test_all_canva_codes_are_registered():
    for code in canva.KNOWN_CODES:
        assert code in registry.REGISTRY


def test_webhook_url_is_never_leaked(story, monkeypatch, capsys):
    monkeypatch.setattr(canva, "transport", Net((500, f"boom {URL}".encode())))
    with pytest.raises(RuntimeError) as info:
        final(story)
    assert URL not in str(info.value)
    assert URL not in (story / "logs" / "thumbnail_attempts.json").read_text(encoding="utf-8")
    assert URL not in capsys.readouterr().out
    assert canva.redact(f"x {URL} y") == "x *** y"


def test_production_path_has_no_overlay_no_other_provider_no_canva_image_api_misuse():
    source = (ROOT / "scripts" / "canva_thumbnail.py").read_text(encoding="utf-8")
    assert "ImageDraw" not in source and "ffmpeg" not in source and ".text(" not in source
    path = (ROOT / "scripts" / "image_generate.py").read_text(encoding="utf-8")
    body = path[path.index("def build_final_thumbnail"):path.index("def reusable_background")]
    for banned in ("render_thumbnail(", "puter", "cloudflare", "pollinations", "bytez", "vercel", "sunburst"):
        assert banned not in body.lower().replace("no cloudflare image, no puter, no pillow text", ""), banned


def test_workflows_pass_the_webhook_secret_via_env_only():
    for name in ("production.yml", "review.yml", "canva-live-test.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "CANVA_THUMBNAIL_WEBHOOK_URL: ${{ secrets.CANVA_THUMBNAIL_WEBHOOK_URL }}" in text
        assert "printenv" not in text and "set -x" not in text
    for name in ("production.yml", "review.yml"):
        assert "PUTER" not in (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_live_test_script_reports_pass_and_keeps_the_image(tmp_path, monkeypatch, capsys):
    import canva_live_test
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(canva.URL_ENV, URL)
    monkeypatch.setattr(canva, "transport", Net())
    monkeypatch.setattr(canva_live_test, "OUT", Path("canva-live-test"))
    assert canva_live_test.main() == 0
    assert "CANVA_LIVE_TEST=PASS size=1280x720" in capsys.readouterr().out
    assert (tmp_path / "canva-live-test" / "canva_thumbnail.jpg").is_file()
    assert json.loads((tmp_path / "canva-live-test" / "result.json").read_text(encoding="utf-8"))["status"] == "PASS"
