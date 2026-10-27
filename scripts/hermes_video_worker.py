import argparse
import random
import subprocess
import time
from pathlib import Path

import google.auth
import gspread
import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

COMFY_DIR = Path("/content/ComfyUI")
COMFY_BASE = "http://127.0.0.1:8188"

NEGATIVE = (
    "low quality, blurry, distorted face, deformed hands, extra fingers, duplicate people, "
    "warped body, flickering, jitter, text, subtitles, watermark, oversaturated colors, "
    "static image, malformed anatomy, bad proportions, compression artifacts"
)


def wait_for_comfy(timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{COMFY_BASE}/system_stats", timeout=3)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def ensure_comfy():
    if wait_for_comfy(3):
        print("✅ ComfyUI already running")
        return

    if not COMFY_DIR.exists():
        raise RuntimeError("/content/ComfyUI not found. Run the Wan2.2 setup cell first.")

    log_path = Path("/content/comfy_worker.log")
    log = open(log_path, "ab", buffering=0)
    subprocess.Popen(
        ["python", "main.py", "--listen", "127.0.0.1", "--lowvram"],
        cwd=str(COMFY_DIR),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    if not wait_for_comfy(180):
        raise RuntimeError(f"ComfyUI did not start. See {log_path}")

    print("✅ ComfyUI server ready")


def profile(aspect_ratio, quality):
    quality = (quality or "balanced").strip().lower()
    aspect = (aspect_ratio or "16:9").strip()

    presets = {
        "fast": {
            "16:9": (640, 352, 12),
            "9:16": (352, 640, 12),
            "1:1": (512, 512, 12),
        },
        "balanced": {
            "16:9": (832, 480, 16),
            "9:16": (480, 832, 16),
            "1:1": (640, 640, 16),
        },
        "high": {
            "16:9": (960, 544, 20),
            "9:16": (544, 960, 20),
            "1:1": (704, 704, 20),
        },
    }

    if quality not in presets:
        quality = "balanced"
    if aspect not in presets[quality]:
        aspect = "16:9"

    width, height, steps = presets[quality][aspect]
    return width, height, steps, quality, aspect


def frame_count(duration_sec, fps=24):
    try:
        duration = float(duration_sec or 4)
    except Exception:
        duration = 4.0
    duration = max(1.0, min(duration, 5.0))
    raw = max(17, int(round(duration * fps)))
    frames = 4 * ((raw - 1) // 4) + 1
    return max(17, min(frames, 121))


def build_graph(prompt_text, duration_sec, aspect_ratio, quality, request_id):
    width, height, steps, quality, aspect = profile(aspect_ratio, quality)
    length = frame_count(duration_sec)
    seed = random.randint(1, 2**63 - 1)

    graph = {
        "37": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "wan2.2_ti2v_5B_fp16.safetensors",
                "weight_dtype": "default",
            },
        },
        "38": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "type": "wan",
                "device": "default",
            },
        },
        "39": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "wan2.2_vae.safetensors"},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt_text, "clip": ["38", 0]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": NEGATIVE, "clip": ["38", 0]},
        },
        "55": {
            "class_type": "Wan22ImageToVideoLatent",
            "inputs": {
                "width": width,
                "height": height,
                "length": length,
                "batch_size": 1,
                "vae": ["39", 0],
            },
        },
        "48": {
            "class_type": "ModelSamplingSD3",
            "inputs": {"shift": 8, "model": ["37", 0]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 5.0,
                "sampler_name": "uni_pc",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["48", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["55", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["39", 0]},
        },
        "57": {
            "class_type": "CreateVideo",
            "inputs": {"fps": 24, "images": ["8", 0]},
        },
        "58": {
            "class_type": "SaveVideo",
            "inputs": {
                "filename_prefix": f"video/HERMES_{request_id}",
                "format": "auto",
                "codec": "auto",
                "video": ["57", 0],
            },
        },
    }

    return graph, {
        "width": width,
        "height": height,
        "steps": steps,
        "frames": length,
        "fps": 24,
        "seed": seed,
        "quality": quality,
        "aspect": aspect,
    }


def submit_and_wait(graph, request_id, timeout_minutes=45):
    r = requests.post(f"{COMFY_BASE}/prompt", json={"prompt": graph}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"ComfyUI rejected job: HTTP {r.status_code}: {r.text[:700]}")

    data = r.json()
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"No prompt_id returned: {data}")

    print(f"🎬 {request_id}: accepted ({prompt_id})")
    deadline = time.time() + timeout_minutes * 60

    while time.time() < deadline:
        h = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=30)
        h.raise_for_status()
        hist = h.json()

        if prompt_id in hist:
            item = hist[prompt_id]
            status = item.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI generation error: {status.get('messages', [])[-3:]}")
            if item.get("outputs") is not None:
                break

        time.sleep(5)
    else:
        raise TimeoutError(f"Generation timed out after {timeout_minutes} minutes")

    out_dir = COMFY_DIR / "output"
    deadline = time.time() + 60
    needle = f"HERMES_{request_id}"

    while time.time() < deadline:
        files = sorted(
            [p for p in out_dir.rglob("*.mp4") if needle in p.name],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if files:
            return files[0]
        time.sleep(2)

    raise FileNotFoundError("Generation completed but output MP4 was not found")


def upload_drive(service, file_path, folder_id):
    body = {"name": file_path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)
    created = service.files().create(
        body=body, media_body=media, fields="id,name,size"
    ).execute()
    return f"https://drive.google.com/file/d/{created['id']}/view"


def update_row(ws, row, status=None, result_url=None, error=None):
    updates = []
    if status is not None:
        updates.append({"range": f"B{row}", "values": [[status]]})
    if result_url is not None:
        updates.append({"range": f"H{row}", "values": [[result_url]]})
    if error is not None:
        updates.append({"range": f"I{row}", "values": [[error[:1000]]]})
    if updates:
        ws.batch_update(updates)


def process(ws, row, record, drive, outbox_folder_id):
    request_id = str(record.get("request_id") or f"row{row}").strip()
    prompt = str(record.get("prompt") or "").strip()
    duration = record.get("duration_sec") or 4
    aspect = str(record.get("aspect_ratio") or "16:9").strip()
    quality = str(record.get("quality") or "balanced").strip()

    if not prompt:
        update_row(ws, row, status="FAILED", error="Missing prompt")
        return

    update_row(ws, row, status="RUNNING", error="")
    print(f"\n▶️ {request_id}: {prompt[:120]}")

    try:
        graph, meta = build_graph(prompt, duration, aspect, quality, request_id)
        print(
            f"   {meta['width']}x{meta['height']} | "
            f"{meta['frames']} frames | {meta['steps']} steps | {meta['quality']}"
        )

        video = submit_and_wait(graph, request_id)
        print(f"✅ Generated: {video}")

        url = upload_drive(drive, video, outbox_folder_id)
        update_row(ws, row, status="COMPLETE", result_url=url, error="")
        print(f"🔗 READY: {url}")

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        update_row(ws, row, status="FAILED", error=msg)
        print(f"❌ {request_id}: {msg}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet-id", required=True)
    parser.add_argument("--outbox-folder-id", required=True)
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()

    creds, _ = google.auth.default()
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(args.sheet_id).sheet1
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    ensure_comfy()

    print("✅ HERMES VIDEO WORKER ONLINE")
    print("Waiting for READY jobs...")

    while True:
        records = ws.get_all_records()
        found = False

        for i, record in enumerate(records, start=2):
            status = str(record.get("status") or "").strip().upper()
            if status in {"READY", "QUEUED", "NEW"}:
                found = True
                process(ws, i, record, drive, args.outbox_folder_id)

        if not found:
            time.sleep(max(2, args.poll_seconds))


if __name__ == "__main__":
    main()
