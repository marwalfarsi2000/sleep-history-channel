"""Minimal Vercel AI Gateway client for the final YouTube thumbnail (reference-image edit, one model only).

One gateway, one model, one secret: nothing else is ever tried. The key is read from the
``AI_GATEWAY_API_KEY`` environment variable (a GitHub Actions secret) and never printed or stored.
Documented endpoints (OpenAI-compatible): ``GET /v1/models`` and ``POST /v1/images/edits``.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE_URL = "https://ai-gateway.vercel.sh/v1"
MODEL = "openai/gpt-image-2.5-sunburst"
SECRET_NAME = "AI_GATEWAY_API_KEY"

KEY_MISSING = "VERCEL_KEY_MISSING"
AUTH_FAILED = "VERCEL_AUTH_FAILED"
FREE_QUOTA_EXHAUSTED = "VERCEL_FREE_QUOTA_EXHAUSTED"
MODEL_UNAVAILABLE = "VERCEL_MODEL_UNAVAILABLE"
BAD_REQUEST = "VERCEL_BAD_REQUEST"
REQUEST_FAILED = "VERCEL_REQUEST_FAILED"
BAD_RESPONSE = "VERCEL_BAD_RESPONSE"
STOP_CODES = (KEY_MISSING, AUTH_FAILED, FREE_QUOTA_EXHAUSTED, MODEL_UNAVAILABLE)  # never retried, never worked around
QUOTA_WORDS = ("credit", "quota", "billing", "insufficient", "payment", "exhaust", "balance", "limit exceeded")


class GatewayError(RuntimeError):
    """``str(error)`` starts with the registered code; it never contains the key or request headers."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def api_key() -> str:
    key = os.environ.get(SECRET_NAME, "").strip()
    if not key:
        raise GatewayError(KEY_MISSING, f"{SECRET_NAME} is not set")
    return key


def transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    """The single network call (replaced in tests). Returns ``(status, body)`` for any HTTP status."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GatewayError(REQUEST_FAILED, type(exc).__name__) from None


def _error_text(body: bytes, key: str = "") -> str:
    if key:
        body = body.replace(key.encode("utf-8"), b"[redacted]")
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    return str(message or "")[:200]


def classify(status: int, body: bytes, key: str = "") -> GatewayError:
    text = _error_text(body, key)
    lowered = text.lower()
    if status == 402 or (status in (400, 403, 429) and any(word in lowered for word in QUOTA_WORDS)):
        return GatewayError(FREE_QUOTA_EXHAUSTED, f"HTTP {status} {text}".strip())
    if status in (401, 403):
        return GatewayError(AUTH_FAILED, f"HTTP {status}")
    if status == 404 and "model" in lowered:
        return GatewayError(MODEL_UNAVAILABLE, f"HTTP {status} {text}".strip())
    if status in (400, 413, 415, 422):
        return GatewayError(BAD_REQUEST, f"HTTP {status} {text}".strip())
    return GatewayError(REQUEST_FAILED, f"HTTP {status}")


def _auth_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def list_model_ids(key: str, timeout: float = 30) -> list[str]:
    status, body = transport("GET", f"{BASE_URL}/models", _auth_headers(key), None, timeout)
    if status != 200:
        raise classify(status, body, key)
    try:
        payload = json.loads(body.decode("utf-8"))
        return [str(item["id"]) for item in payload["data"]]
    except (ValueError, KeyError, TypeError):
        raise GatewayError(BAD_RESPONSE, "models list is not valid JSON") from None


def check_connection(key: str) -> dict:
    """Authenticated ``GET /v1/models`` only; proves the key works and the exact model is listed. Creates no image."""
    result = {"VERCEL_AUTH": "FAIL", "SUNBURST_MODEL": "NOT_TESTED"}
    try:
        ids = list_model_ids(key)
    except GatewayError as exc:
        result["VERCEL_ERROR"] = exc.code
        return result
    result["VERCEL_AUTH"] = "PASS"
    result["SUNBURST_MODEL"] = "AVAILABLE" if MODEL in ids else "UNAVAILABLE"
    if MODEL not in ids:
        result["VERCEL_ERROR"] = MODEL_UNAVAILABLE
    return result


def encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    lines: list[bytes] = []
    for name, value in fields.items():
        lines += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{name}"'.encode(), b"", value.encode("utf-8")]
    for name, (filename, content, content_type) in files.items():
        lines += [f"--{boundary}".encode(),
                  f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(),
                  f"Content-Type: {content_type}".encode(), b"", content]
    lines += [f"--{boundary}--".encode(), b""]
    return f"multipart/form-data; boundary={boundary}", b"\r\n".join(lines)


def edit_image(key: str, prompt: str, reference_png: bytes, size: str, timeout: float = 600) -> bytes:
    """One billable request: the reference image plus the prompt, fixed model, returns the generated image bytes."""
    content_type, body = encode_multipart(
        {"model": MODEL, "prompt": prompt, "size": size, "n": "1"},
        {"image": ("reference.png", reference_png, "image/png")})
    headers = {**_auth_headers(key), "Content-Type": content_type}
    status, response = transport("POST", f"{BASE_URL}/images/edits", headers, body, timeout)
    if status != 200:
        raise classify(status, response, key)
    try:
        item = json.loads(response.decode("utf-8"))["data"][0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            status, image = transport("GET", item["url"], {}, None, timeout)
            if status == 200 and image:
                return image
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    raise GatewayError(BAD_RESPONSE, "no image in the response")


def main(argv: list[str] | None = None) -> int:
    """``python scripts/vercel_gateway.py check``: prints only status codes (used by the smoke-test workflow)."""
    command = (argv if argv is not None else sys.argv[1:] or [""])[0]
    if command != "check":
        print("usage: vercel_gateway.py check", file=sys.stderr)
        return 2
    try:
        key = api_key()
    except GatewayError as exc:
        print(f"VERCEL_AUTH=FAIL\nVERCEL_ERROR={exc.code}")
        return 1
    result = check_connection(key)
    for name, value in result.items():
        print(f"{name}={value}")
    return 0 if result["VERCEL_AUTH"] == "PASS" and result["SUNBURST_MODEL"] == "AVAILABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
