import base64
import io
import json
import re
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import sunburst_live_test as live  # noqa: E402
import sunburst_thumbnail as sunburst  # noqa: E402
import vercel_gateway as gateway  # noqa: E402


def png(size):
    buffer = io.BytesIO()
    Image.effect_noise(size, 70).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def frame(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key-0123456789")
    path = tmp_path / "frame.png"
    path.write_bytes(png((1920, 1080)))
    return path


def test_exactly_one_request_and_a_reviewable_result(frame, monkeypatch, capsys):
    calls = []

    def net(method, url, headers, body, timeout):
        calls.append((method, url))
        return 200, json.dumps({"data": [{"b64_json": base64.b64encode(png((1920, 1080))).decode()}]}).encode()
    monkeypatch.setattr(gateway, "transport", net)
    assert live.main([str(frame)]) == 0
    assert calls == [("POST", "https://ai-gateway.vercel.sh/v1/images/edits")]
    out = Path("sunburst-live-test")
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "PASS" and result["final_size"] == [1920, 1080] and result["background_unchanged"]
    assert result["model"] == "openai/gpt-image-2.5-sunburst" and result["paid_fallback_used"] is False
    assert (out / "2_sunburst_thumbnail.jpg").is_file() and (out / "1_reference_background.png").is_file()
    assert "test-key" not in json.dumps(result)


def test_failure_is_reported_once_without_retry(frame, monkeypatch):
    calls = []

    def net(method, url, headers, body, timeout):
        calls.append(url)
        return 400, json.dumps({"error": {"message": "unsupported parameter"}}).encode()
    monkeypatch.setattr(gateway, "transport", net)
    assert live.main([str(frame)]) == 1
    assert len(calls) == 1
    assert json.loads(Path("sunburst-live-test/result.json").read_text(encoding="utf-8"))["error_code"] == "VERCEL_BAD_REQUEST"


def test_workflow_is_manual_single_attempt_and_read_only():
    text = (ROOT / ".github" / "workflows" / "sunburst-live-test.yml").read_text(encoding="utf-8")
    assert re.search(r"^on:\n  workflow_dispatch:", text, re.M) and "schedule" not in text and "push:" not in text
    assert "permissions:\n  contents: read" in text and "AI_GATEWAY_API_KEY: ${{ secrets.AI_GATEWAY_API_KEY }}" in text
    assert text.count("sunburst_live_test.py") == 1 and "uses: actions/upload-artifact@v4" in text
    assert live.sunburst.MAX_SUNBURST_THUMBNAIL_ATTEMPTS == 4 and "max_attempts=1" in Path(live.__file__).read_text(encoding="utf-8")
