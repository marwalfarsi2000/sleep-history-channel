"""Final YouTube thumbnail made entirely inside Canva through the owner's n8n workflow.

Canva Lucid Origin generates a text-free background, the fixed 1280x720 brand template is autofilled (BACKGROUND and
HOOK_TEXT, with the Arabic text styled by the template itself) and Canva exports the PNG. OAuth and token refresh live in
n8n. There is no fallback: any failure raises a ``CANVA_*`` error. No code here draws text or resizes: the export is
validated and only re-encoded as JPEG for YouTube. ``10_background.png`` (the video background, made elsewhere) is never
touched. The webhook URL is a secret (CANVA_THUMBNAIL_WEBHOOK_URL) and is never logged.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

URL_ENV = "CANVA_THUMBNAIL_WEBHOOK_URL"
BACKGROUND_FILE = "10_background.png"
THUMBNAIL_FILE = "11_thumbnail_base.jpg"
OUTPUT_SIZE = (1280, 720)
TEMPLATE = "Canva brand template EAHYe4Y0z-s (BACKGROUND, HOOK_TEXT)"
BACKGROUND_MODEL = "lucid_origin"
MAX_ATTEMPTS = 2  # only for an unreachable workflow; a Canva-side failure is never retried (it costs generation credits)
PROMPT_MIN, PROMPT_MAX = 20, 1900
KNOWN_CODES = (
    "CANVA_BACKGROUND_GENERATION_FAILED", "CANVA_AUTOFILL_FAILED", "CANVA_EXPORT_FAILED",
    "CANVA_AUTH_MISSING", "CANVA_TEMPLATE_INVALID", "CANVA_THUMBNAIL_FAILED",
)
BACKGROUND_RULES = (
    "Thumbnail background only: calm cinematic composition with one clear focal point, soft warm lighting and natural "
    "empty space on the right side of the frame for a title. Absolutely no text, letters, numbers, writing, signs, logos, "
    "frames, boxes, panels or dark patches."
)
TIMEOUT_SECONDS = 300
YOUTUBE_THUMBNAIL_MAX_BYTES = 2_000_000
JPEG_QUALITIES = (95, 92, 88, 84, 80, 75, 70)
ATTEMPT_LOG = "thumbnail_attempts.json"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


class CanvaError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def enabled() -> bool:
    return bool(os.environ.get(URL_ENV))


def redact(text: str) -> str:
    url = os.environ.get(URL_ENV, "")
    return text.replace(url, "***") if url else text


def transport(url: str, payload: bytes, timeout: int) -> tuple[int, bytes]:
    """POST JSON, return (status, body). Patched in tests."""
    request = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https URL from a secret
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CanvaError("CANVA_UNREACHABLE", redact(type(exc).__name__)) from exc


def build_background_prompt(thumbnail_prompt: str, scene_prompt: str = "") -> str:
    """The Lucid Origin prompt: the packaging step's thumbnail_prompt (or the episode scene prompt) plus text-free rules."""
    base = (thumbnail_prompt or scene_prompt or "").strip()
    if not base:
        raise CanvaError("CANVA_BACKGROUND_GENERATION_FAILED", "no background prompt is available")
    room = PROMPT_MAX - len(BACKGROUND_RULES) - 2
    return f"{base[:room].rstrip()}\n\n{BACKGROUND_RULES}"


def decode_export(data: bytes) -> Image.Image:
    """The export must be a real PNG/JPEG of exactly 1280x720 (not an HTML or JSON error page)."""
    if not data:
        raise ValueError("empty response")
    if not (data.startswith(PNG_MAGIC) or data.startswith(JPEG_MAGIC)):
        raise ValueError("response is not a PNG/JPEG image")
    image = Image.open(io.BytesIO(data))
    image.load()
    image = image.convert("RGB")
    if image.size != OUTPUT_SIZE:
        raise ValueError(f"unexpected export size {image.size}")
    return image


def validate(image: Image.Image) -> None:
    gray = image.convert("L")
    if ImageStat.Stat(gray).stddev[0] < 18 or ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0] < 2.0:
        raise ValueError("exported thumbnail looks flat or blank")


def save_jpeg(image: Image.Image, path: Path) -> int:
    for quality in JPEG_QUALITIES:
        image.save(path, format="JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= YOUTUBE_THUMBNAIL_MAX_BYTES:
            return quality
    raise RuntimeError("thumbnail exceeds YouTube's 2 MB limit even at the lowest JPEG quality")


def _log(story: Path, record: dict) -> None:
    path = story / "logs" / ATTEMPT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except ValueError:
        records = []
    records.append(record)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _error_code(body: bytes) -> str:
    try:
        code = str(json.loads(body.decode("utf-8", "replace")).get("error", ""))
    except (ValueError, AttributeError):
        return ""
    return code if code in KNOWN_CODES else ""


def request_export(background_prompt: str, hook_text: str, timeout: int = TIMEOUT_SECONDS) -> bytes:
    url = os.environ.get(URL_ENV, "")
    if not url:
        raise CanvaError("CANVA_AUTH_MISSING", f"{URL_ENV} is not set")
    payload = json.dumps({"hook_text": hook_text, "background_prompt": background_prompt}).encode()
    status, body = transport(url, payload, timeout)
    if status in (401, 403):
        raise CanvaError("CANVA_AUTH_MISSING", f"HTTP {status}")
    if status >= 400:
        raise CanvaError(_error_code(body) or "CANVA_THUMBNAIL_FAILED", f"HTTP {status}")
    return body


def generate(story: Path, thumbnail_text: str, background_prompt: str, *, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Produce ``11_thumbnail_base.jpg`` entirely through Canva; raises a CANVA_* error and never falls back."""
    if len(re.findall(r"[ء-ي]+", thumbnail_text)) not in (2, 3, 4):
        raise CanvaError("CANVA_THUMBNAIL_FAILED", "hook text must be 2-4 Arabic words")
    if not PROMPT_MIN <= len(background_prompt) <= PROMPT_MAX:
        raise CanvaError("CANVA_BACKGROUND_GENERATION_FAILED", "background prompt length is out of range")
    last: CanvaError | None = None
    for number in range(1, min(int(max_attempts), MAX_ATTEMPTS) + 1):
        record = {"attempt": number, "provider": "canva_via_n8n", "background_model": BACKGROUND_MODEL}
        try:
            body = request_export(background_prompt, thumbnail_text)
            code = _error_code(body)
            if code:
                raise CanvaError(code)
            try:
                image = decode_export(body)
                validate(image)
            except (OSError, ValueError) as exc:
                raise CanvaError("CANVA_EXPORT_FAILED", f"invalid export: {str(exc)[:100]}") from exc
        except CanvaError as exc:
            _log(story, {**record, "outcome": "error", "code": exc.code})
            last = exc
            if exc.code == "CANVA_UNREACHABLE":
                time.sleep(min(10 * number, 30))
                continue
            raise
        quality = save_jpeg(image, story / THUMBNAIL_FILE)
        _log(story, {**record, "outcome": "passed"})
        return {
            "thumbnail_generation_provider": "canva_via_n8n",
            "thumbnail_background_model": BACKGROUND_MODEL,
            "thumbnail_template": TEMPLATE,
            "thumbnail_text_render_method": "canva_autofill_template",
            "thumbnail_attempts": number,
            "thumbnail_size": f"{OUTPUT_SIZE[0]}x{OUTPUT_SIZE[1]}",
            "thumbnail_jpeg_quality": quality,
            "thumbnail_text_ocr_verified": False,  # no reliable Arabic OCR here: owner review is the final text check
        }
    raise CanvaError("CANVA_THUMBNAIL_FAILED", f"workflow unreachable ({last.code if last else 'no attempt'})")
