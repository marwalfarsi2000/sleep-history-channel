from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import socket
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
IMAGE_LOG = ROOT / "logs" / "image_generation.jsonl"


def safe_log(provider: str, model: str, success: bool, latency_ms: int, error_code: str | None = None) -> None:
    IMAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "provider": provider,
        "model": model,
        "success": success,
        "latency_ms": latency_ms,
    }
    if error_code:
        record["error_code"] = error_code
    with IMAGE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_secrets() -> dict[str, str]:
    values = dict(os.environ)
    path = Path(os.environ.get(
        "SLEEP_HISTORY_SECRETS_FILE",
        str(Path.home() / "AppData/Local/SleepHistoryAutomation/secrets.env"),
    ))
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                if not values.get(key):
                    values[key] = value.strip()
    return values


def model_record(model_id: str) -> dict:
    request = urllib.request.Request(
        "https://gen.pollinations.ai/image/models",
        headers={"User-Agent": "SleepHistoryAutomation/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        catalog = json.loads(response.read().decode("utf-8"))
    if isinstance(catalog, str):
        catalog = json.loads(catalog)
    for item in catalog:
        if item.get("name") == model_id or model_id in item.get("aliases", []):
            return item
    raise RuntimeError(f"Image model is not present in Pollinations catalog: {model_id}")


def key_record(api_key: str) -> dict:
    request = urllib.request.Request(
        "https://gen.pollinations.ai/account/key",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "SleepHistoryAutomation/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def enforce_free_model(model_id: str, api_key: str) -> dict:
    item = model_record(model_id)
    if item.get("paid_only"):
        raise RuntimeError(f"BLOCKED_COST: image model is marked paid_only: {model_id}")
    key_info = key_record(api_key)
    permissions = key_info.get("permissions") or {}
    if not key_info.get("valid"):
        raise RuntimeError("Pollinations image key is not valid")
    if "image" not in (permissions.get("models") or []):
        raise RuntimeError("Pollinations key is not restricted to an allowed image model category")
    if SETTINGS.get("pollinations_cost_mode", SETTINGS.get("image_cost_mode")) != "quest_pollen_only":
        raise RuntimeError("BLOCKED_COST: pollinations_cost_mode must remain quest_pollen_only")
    if not key_info.get("questPollenOnly"):
        raise RuntimeError("BLOCKED_COST: Pollinations key could spend non-Quest Pollen")
    return item


def validate_image(image: Image.Image) -> None:
    if image.size != (1920, 1080):
        raise RuntimeError(f"Unexpected image size: {image.size}")
    gray = image.convert("L")
    contrast = ImageStat.Stat(gray).stddev[0]
    edge_mean = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]
    if contrast < 18 or edge_mean < 2.0:
        raise RuntimeError(
            f"Generated image failed visual-quality guard (contrast={contrast:.1f}, edges={edge_mean:.1f})"
        )


def prepare_image(image_bytes: bytes, temporary: Path) -> Image.Image:
    """Normalize provider output and reject unusable images before accepting it."""
    temporary.write_bytes(image_bytes)
    with Image.open(temporary) as source:
        image = source.convert("RGB")
    image = ImageOps.fit(
        image,
        (1920, 1080),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    image = ImageEnhance.Contrast(image).enhance(1.03)
    image = ImageEnhance.Sharpness(image).enhance(1.06)
    validate_image(image)
    return image


def historical_accuracy_gate(image_path: Path, prompt: str, secrets_values: dict[str, str]) -> dict:
    api_key = secrets_values.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("IMAGE_ACCURACY_GATE_FAILED: GEMINI_API_KEY is missing")
    model = str(SETTINGS.get("image_accuracy_gate_model", SETTINGS.get("llm_model", "gemini-3.1-flash-lite")))
    if SETTINGS.get("cost_policy") != "free_or_preapproved_only" or model not in set(SETTINGS.get("approved_llm_models") or []):
        raise RuntimeError(f"BLOCKED_COST: unapproved image accuracy model {model}")
    payload = {
        "contents": [{"role": "user", "parts": [
            {"text": (
                "Act as a strict historical-image quality gate. Compare the image with the requested "
                "historical scene. Reject visible modern objects, wrong-period architecture/clothing/tools, "
                "fantasy elements, text, logos, watermarks, black bars, severe anatomy defects, or a scene "
                "that clearly contradicts the prompt. Return JSON only: "
                "{\"status\":\"PASS or FAIL\",\"historically_plausible\":true,"
                "\"no_modern_objects\":true,\"no_text_or_watermark\":true,"
                "\"topic_match\":true,\"issues\":[]}.\nPrompt: " + prompt
            )},
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_path.read_bytes()).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1200, "responseMimeType": "application/json"},
    }
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
        text = "".join(
            part.get("text", "")
            for part in result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        )
        gate = json.loads(text)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError, IndexError) as exc:
        raise RuntimeError(f"IMAGE_ACCURACY_GATE_FAILED: {type(exc).__name__}") from exc
    required = ("historically_plausible", "no_modern_objects", "no_text_or_watermark", "topic_match")
    if gate.get("status") != "PASS" or not all(gate.get(key) is True for key in required):
        raise RuntimeError("IMAGE_ACCURACY_GATE_FAILED: " + json.dumps(gate, ensure_ascii=False))
    return gate


def image_provider_label() -> str:
    provider = SETTINGS.get("image_provider", "pollinations")
    if provider == "cloudflare_workers_ai":
        model_id = SETTINGS.get("image_model", "")
        cost_mode = SETTINGS.get("image_cost_mode", "")
    elif provider == "bytez":
        model_id = SETTINGS.get("bytez_image_model", "")
        cost_mode = SETTINGS.get("bytez_cost_mode", "")
    else:
        model_id = SETTINGS.get("pollinations_image_model", SETTINGS.get("image_model", ""))
        cost_mode = SETTINGS.get("pollinations_cost_mode", SETTINGS.get("image_cost_mode", ""))
    return f"{provider} {model_id} ({cost_mode})"


def _extract_bytez_image(output: object) -> bytes:
    candidate = output
    if isinstance(candidate, list) and candidate:
        candidate = candidate[0]
    if isinstance(candidate, dict):
        for key in ("url", "image", "output"):
            if candidate.get(key):
                candidate = candidate[key]
                break
    if not isinstance(candidate, str) or not candidate.strip():
        raise RuntimeError("Bytez returned no usable image output")
    candidate = candidate.strip()
    if candidate.startswith("data:image/") and "," in candidate:
        return base64.b64decode(candidate.split(",", 1)[1])
    if candidate.startswith("http://") or candidate.startswith("https://"):
        request = urllib.request.Request(
            candidate,
            headers={"User-Agent": "SleepHistoryAutomation/1.0"},
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.read()
    try:
        return base64.b64decode(candidate, validate=True)
    except ValueError as exc:
        raise RuntimeError("Bytez returned an unsupported image payload") from exc


def generate_bytez(prompt: str, api_key: str) -> tuple[bytes, dict]:
    if SETTINGS.get("bytez_cost_mode") != "free_or_manually_funded_credits_only":
        raise RuntimeError(
            "BLOCKED_COST: Bytez image_cost_mode must remain free_or_manually_funded_credits_only"
        )
    if SETTINGS.get("image_fallback_provider") is not None:
        raise RuntimeError("Bytez integration requires image_fallback_provider=null")
    model_id = SETTINGS.get("bytez_image_model", "fal/FLUX.2-dev-Turbo")
    encoded_model = "/".join(urllib.parse.quote(part, safe="") for part in model_id.split("/"))
    request = urllib.request.Request(
        f"https://api.bytez.com/models/v2/{encoded_model}",
        data=json.dumps(
            {
                "text": prompt,
                "params": {
                    "width": 1344,
                    "height": 768,
                    "num_inference_steps": 8,
                },
            }
        ).encode("utf-8"),
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "User-Agent": "SleepHistoryAutomation/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in (402, 403):
            raise RuntimeError(
                "BLOCKED_COST: Bytez plan does not currently allow fal/FLUX.2-dev-Turbo"
            ) from exc
        raise RuntimeError(f"Bytez image request failed with HTTP {exc.code}: {body[:300]}") from exc
    if payload.get("error"):
        message = str(payload["error"])
        if any(word in message.lower() for word in ("credit", "billing", "payment", "plan", "unlock")):
            raise RuntimeError(f"BLOCKED_COST: Bytez rejected the model: {message}")
        raise RuntimeError(f"Bytez image generation failed: {message}")
    return _extract_bytez_image(payload.get("output")), payload


def _cloudflare_error(payload: object) -> tuple[str | None, str]:
    if not isinstance(payload, dict):
        return None, "Invalid Cloudflare error response"
    errors = payload.get("errors") or []
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
        code = str(first.get("code")) if first.get("code") is not None else None
        return code, str(first.get("message") or "Cloudflare Workers AI request failed")
    return None, str(payload.get("message") or "Cloudflare Workers AI request failed")


def _extract_cloudflare_image(payload: object) -> bytes:
    if not isinstance(payload, dict) or payload.get("success") is False:
        code, message = _cloudflare_error(payload)
        raise RuntimeError(f"CLOUDFLARE_ERROR[{code or 'unknown'}]: {message}")
    result = payload.get("result", payload)
    image_value = result.get("image") if isinstance(result, dict) else None
    if not isinstance(image_value, str) or not image_value.strip():
        raise RuntimeError("CLOUDFLARE_INVALID_RESPONSE: missing image/base64")
    encoded = "".join(image_value.split())
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise RuntimeError("CLOUDFLARE_INVALID_RESPONSE: invalid image/base64") from exc
    if not image_bytes:
        raise RuntimeError("CLOUDFLARE_INVALID_RESPONSE: empty decoded image")
    return image_bytes


def generate_cloudflare(prompt: str, account_id: str, api_token: str) -> tuple[bytes, dict]:
    model_id = SETTINGS.get("image_model", "@cf/black-forest-labs/flux-1-schnell")
    steps = int(SETTINGS.get("image_steps", 4))
    if not 1 <= steps <= 8:
        raise RuntimeError("Cloudflare image_steps must be between 1 and 8")
    if not prompt or len(prompt) > 2048:
        raise RuntimeError(
            f"Cloudflare prompt must be 1-2048 characters; refusing to truncate ({len(prompt)} characters)"
        )
    endpoint = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{urllib.parse.quote(account_id, safe='')}/ai/run/{model_id}"
    )
    body = json.dumps({"prompt": prompt, "steps": steps}).encode("utf-8")
    started = time.monotonic()
    last_error: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "SleepHistoryAutomation/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            image_bytes = _extract_cloudflare_image(payload)
            safe_log("cloudflare_workers_ai", model_id, True, int((time.monotonic() - started) * 1000))
            return image_bytes, payload
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"message": f"HTTP {exc.code}"}
            code, message = _cloudflare_error(payload)
            latency = int((time.monotonic() - started) * 1000)
            if exc.code in (401, 403):
                label = "AUTH" if code != "5035" else "PAID_PLAN_REQUIRED"
                safe_log("cloudflare_workers_ai", model_id, False, latency, code or label)
                prefix = "BLOCKED_COST" if code == "5035" else "CLOUDFLARE_AUTH_ERROR"
                raise RuntimeError(f"{prefix}[{code or exc.code}]: {message}") from exc
            if exc.code == 429 and code == "3036":
                safe_log("cloudflare_workers_ai", model_id, False, latency, code)
                raise RuntimeError(f"BLOCKED_COST[3036]: Workers AI free allocation exhausted: {message}") from exc
            if exc.code == 429 and attempt < 2:
                last_error = exc
                time.sleep(2**attempt)
                continue
            # Cloudflare code 8007 is an upstream Workers AI execution
            # failure. It proved transient in production, so retry it (and
            # other server-side 5xx failures) before using the configured
            # image fallback.
            if (code == "8007" or exc.code >= 500) and attempt < 2:
                last_error = exc
                time.sleep(2**attempt)
                continue
            safe_log("cloudflare_workers_ai", model_id, False, latency, code or str(exc.code))
            raise RuntimeError(f"CLOUDFLARE_HTTP_ERROR[{code or exc.code}]: {message}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            safe_log(
                "cloudflare_workers_ai",
                model_id,
                False,
                int((time.monotonic() - started) * 1000),
                "NETWORK_OR_TIMEOUT",
            )
            raise RuntimeError("CLOUDFLARE_NETWORK_ERROR: timeout or network failure") from exc
        except (json.JSONDecodeError, RuntimeError) as exc:
            safe_log(
                "cloudflare_workers_ai",
                model_id,
                False,
                int((time.monotonic() - started) * 1000),
                "INVALID_RESPONSE",
            )
            raise
    raise RuntimeError(f"CLOUDFLARE_NETWORK_ERROR: {last_error}")


def generate_pollinations(prompt: str, api_key: str, seed: int) -> tuple[bytes, dict]:
    model_id = SETTINGS.get("pollinations_image_model", "microsoft/mai-image-2.6")
    model = enforce_free_model(model_id, api_key)
    encoded_prompt = urllib.parse.quote(prompt, safe="")
    query = urllib.parse.urlencode(
        {
            "model": model_id,
            "width": 1344,
            "height": 768,
            "seed": seed,
            "nologo": "true",
        }
    )
    url = f"https://gen.pollinations.ai/image/{encoded_prompt}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "SleepHistoryAutomation/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            raise RuntimeError(f"Unexpected Pollinations response: {content_type}")
        return response.read(), model


def generate(story: Path, seed: int | None = None) -> None:
    secret_values = load_secrets()
    provider = SETTINGS.get("image_provider", "pollinations")

    prompt_path = story / "08_image_prompt_en.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(prompt_path)
    if provider == "cloudflare_workers_ai":
        model_id = SETTINGS.get("image_model", "@cf/black-forest-labs/flux-1-schnell")
    elif provider == "bytez":
        model_id = SETTINGS.get("bytez_image_model", "fal/FLUX.2-dev-Turbo")
    else:
        model_id = SETTINGS.get("pollinations_image_model", "microsoft/mai-image-2.6")

    original_prompt = prompt_path.read_text(encoding="utf-8").strip()
    legacy_prompt = (
        "Premium photorealistic editorial historical reconstruction, museum-quality material accuracy, "
        "natural human proportions, cinematic wide establishing shot, realistic architectural depth, "
        "subtle filmic lighting, detailed tactile materials, professional color grading, full-frame 16:9 composition. "
        + original_prompt
        + " Avoid cartoon, vector art, primitive shapes, clipart, fantasy, modern objects, distorted anatomy, "
        "purple tint, blue tint, text, logos, watermarks, borders, letterboxing and black bars."
    )
    seed = seed if seed is not None else secrets.randbelow(2_147_483_647)
    temporary = story / "10_background.generating"
    try:
        actual_provider = provider
        actual_model = model_id
        actual_cost_mode = SETTINGS.get("image_cost_mode")
        try:
            if provider == "cloudflare_workers_ai":
                account_id = secret_values.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
                api_token = secret_values.get("CLOUDFLARE_API_TOKEN", "").strip()
                if not account_id or not api_token:
                    raise RuntimeError(
                        "CLOUDFLARE_CREDENTIALS_MISSING: CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required"
                    )
                image_bytes, provider_record = generate_cloudflare(original_prompt, account_id, api_token)
            elif provider == "bytez":
                api_key = secret_values.get("BYTEZ_API_KEY", "").strip()
                if not api_key:
                    raise RuntimeError("BYTEZ_API_KEY is missing from the external secrets store")
                image_bytes, provider_record = generate_bytez(legacy_prompt, api_key)
            elif provider == "pollinations":
                api_key = secret_values.get("POLLINATIONS_API_KEY", "").strip()
                if not api_key:
                    raise RuntimeError("POLLINATIONS_API_KEY is missing from the external secrets store")
                image_bytes, provider_record = generate_pollinations(legacy_prompt, api_key, seed)
            else:
                raise RuntimeError(f"Unsupported image_provider: {provider}")
            image = prepare_image(image_bytes, temporary)
        except RuntimeError:
            fallback = SETTINGS.get("image_fallback_provider")
            if provider != "cloudflare_workers_ai" or fallback != "pollinations":
                raise
            api_key = secret_values.get("POLLINATIONS_API_KEY", "").strip()
            if not api_key:
                raise
            actual_provider = "pollinations"
            actual_model = SETTINGS.get("pollinations_image_model", "microsoft/mai-image-2.6")
            actual_cost_mode = SETTINGS.get("pollinations_cost_mode", "quest_pollen_only")
            image_bytes, provider_record = generate_pollinations(legacy_prompt, api_key, seed)
            image = prepare_image(image_bytes, temporary)
        background = story / "10_background.png"
        thumbnail = story / "11_thumbnail_base.jpg"
        image.save(background, format="PNG", optimize=True)
        image.resize((1280, 720), Image.Resampling.LANCZOS).save(
            thumbnail, format="JPEG", quality=94, optimize=True
        )
        accuracy = historical_accuracy_gate(background, original_prompt, secret_values)
    finally:
        temporary.unlink(missing_ok=True)

    (story / "09_image_source.md").write_text(
        "# مصدر الصورة\n\n"
        f"- الطريقة: توليد تلقائي عبر `{actual_provider}`.\n"
        f"- النموذج: `{actual_model}`.\n"
        f"- Seed محلي للفallback فقط: `{seed}`.\n"
        f"- سياسة التكلفة: `{actual_cost_mode}`.\n"
        "- ملاحظة تاريخية: إعادة بناء بصرية مولدة آليًا وليست مرجعًا أثريًا حرفيًا.\n",
        encoding="utf-8",
    )
    metadata_path = story / "15_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update({
        "image_provider": actual_provider,
        "image_model": actual_model,
        "image_generation_method": "ai_generated_historical_reconstruction",
        "image_generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "image_path": str(background),
        "thumbnail_path": str(thumbnail),
        "image_generation_status": "passed",
        "image_accuracy_status": "passed",
        "image_accuracy_gate": accuracy,
        "image_prompt_sha256": hashlib.sha256(original_prompt.encode("utf-8")).hexdigest(),
        "image_sha256": hashlib.sha256(background.read_bytes()).hexdigest(),
    })
    temporary_metadata = metadata_path.with_name(metadata_path.name + ".tmp")
    temporary_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary_metadata.replace(metadata_path)
    print(json.dumps({"status": "ok", "provider": actual_provider, "model": actual_model, "seed": seed}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("story")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    generate(Path(args.story).resolve(), args.seed)


if __name__ == "__main__":
    main()
