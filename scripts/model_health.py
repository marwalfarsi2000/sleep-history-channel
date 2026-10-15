"""Monthly check that every configured Gemini and Google Cloud TTS model still exists.

Reports retired models, newly listed candidates, and Cloud TTS usage against the
free-tier caps. Exits non-zero when a configured model is no longer available.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))


def get_json(url: str, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def gemini_models(api_key: str) -> set[str]:
    names, token = set(), ""
    while True:
        query = urllib.parse.urlencode({"pageSize": 1000, **({"pageToken": token} if token else {})})
        data = get_json(
            f"https://generativelanguage.googleapis.com/v1beta/models?{query}",
            {"x-goog-api-key": api_key},
        )
        names.update(item["name"].removeprefix("models/") for item in data.get("models", []))
        token = data.get("nextPageToken", "")
        if not token:
            return names


def configured_models() -> dict[str, list[str]]:
    return {
        "writing": [SETTINGS["llm_model"], *SETTINGS.get("llm_fallback_models", [])],
        "research": SETTINGS.get("research_fallback_models", []),
        "tts": [SETTINGS["gemini_tts_model"], *SETTINGS.get("gemini_tts_fallback_models", [])],
        "audio_qc": [SETTINGS.get("audio_qc_model", "")],
    }


def cloud_tts_report(api_key: str) -> list[str]:
    config = SETTINGS.get("google_cloud_tts") or {}
    voices = get_json(
        f"https://texttospeech.googleapis.com/v1/voices?languageCode={config.get('language_code', 'ar-XA')}",
        {"x-goog-api-key": api_key},
    )
    available = {voice["name"] for voice in voices.get("voices", [])}
    usage_path = ROOT / "config" / "tts_usage.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8")).get(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"), {}) if usage_path.exists() else {}
    lines = []
    for voice in config.get("voices", []):
        state = "available" if voice["name"] in available else "MISSING"
        cap = (config.get("monthly_char_caps") or {}).get(voice["tier"], 0)
        lines.append(f"Cloud TTS {voice['name']}: {state}; used {usage.get(voice['tier'], 0)}/{cap} {voice['tier']} chars this month")
    return lines


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("GEMINI_API_KEY is missing", file=sys.stderr)
        return 2
    available = gemini_models(api_key)
    missing, lines = [], []
    for group, models in configured_models().items():
        for model in filter(None, models):
            ok = model in available
            if not ok:
                missing.append(model)
            lines.append(f"{group}: {model} {'available' if ok else 'MISSING'}")
    configured = {model for models in configured_models().values() for model in models}
    candidates = sorted(
        name for name in available
        if name.startswith("gemini-") and ("flash" in name or "tts" in name) and name not in configured
    )
    if candidates:
        lines.append("New or unused candidates: " + ", ".join(candidates))
    cloud_key = os.environ.get("GOOGLE_CLOUD_TTS_API_KEY", "")
    if cloud_key:
        lines.extend(cloud_tts_report(cloud_key))
    else:
        lines.append("Cloud TTS: GOOGLE_CLOUD_TTS_API_KEY not configured; last-resort voice stage is disabled")
    report = "\n".join(lines)
    print(report)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        title = "⚠️ نماذج متوقفة تحتاج صيانة" if missing else "✅ فحص النماذج الشهري"
        data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n\n{report}"[:4000]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=60).close()
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
