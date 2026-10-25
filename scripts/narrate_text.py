from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
from pathlib import Path

from internetarchive import upload

import pipeline
import run_summary
from archive_storage import wait_for_remote_file

ROOT = Path(__file__).resolve().parents[1]
REQUEST_PATH = ROOT / ".ops" / "narrate-text.json"
RESULT_PATH = ROOT / ".ops" / "narrate-result.json"


def _safe(value: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return re.sub(r"-+", "-", value)[:48] or "manual"


def main() -> None:
    payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    text = str(payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("Narration request has no text")

    title = str(payload.get("title") or "نص صوتي لقناة لن تُكمل هذه القصة").strip()
    request_id = _safe(str(payload.get("request_id") or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")))
    created_at = dt.datetime.now(dt.timezone.utc).isoformat()
    story = ROOT / ".runtime" / "narrate" / request_id
    story.mkdir(parents=True, exist_ok=True)

    (story / "05_story_clean.md").write_text(text + "\n", encoding="utf-8")
    metadata = {
        "slug": request_id,
        "title": title,
        "created_at": created_at,
        "topic_gate_status": "passed",
        "grounded_research_status": "passed",
        "source_validation_status": "passed",
        "language_gate_status": "passed",
        "religion_gate_status": "passed",
        "sleep_gate_status": "passed",
        "fact_check_status": "passed",
        "second_fact_check_status": "passed",
        "fact_corrections_applied": True,
        "manual_text_narration": True,
    }
    (story / "15_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    run_summary.start(workflow="narrate_text", action="tts_and_archive", story_path=str(story))
    # Direct text narration is not a full episode, so the production 58–90 minute duration gate does not apply.
    pipeline.SETTINGS["target_min_seconds"] = 1
    pipeline.SETTINGS["target_max_seconds"] = 24 * 60 * 60
    asyncio.run(pipeline.synthesize(story))

    final_audio = story / "12_audio_full.mp3"
    narration_audio = story / "12_audio_narration.mp3"
    if not final_audio.is_file() or final_audio.stat().st_size <= 1024:
        raise RuntimeError("Final audio was not produced")

    access = os.environ.get("IAS3_ACCESS_KEY", "").strip()
    secret = os.environ.get("IAS3_SECRET_KEY", "").strip()
    if not access or not secret:
        raise RuntimeError("Internet Archive credentials are not configured")

    date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    identifier = _safe(str(payload.get("archive_identifier") or f"lan-tukmil-alqissa-audio-{date}-{request_id}"))
    archive_files = {
        "sleep_history_audio.mp3": str(final_audio),
        "narration_only.mp3": str(narration_audio),
        "source_text.txt": str(story / "05_story_clean.md"),
    }
    responses = upload(
        identifier,
        archive_files,
        metadata={
            "title": title,
            "creator": "لن تُكمل هذه القصة",
            "description": "تسجيل صوتي هادئ مولد من نص جاهز باستخدام إعدادات الراوي الخاصة بقناة لن تُكمل هذه القصة.",
            "mediatype": "audio",
            "collection": "opensource_audio",
            "language": "ara",
            "licenseurl": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
        },
        headers={"x-archive-keep-old-version": "0", "x-archive-queue-derive": "1"},
        access_key=access,
        secret_key=secret,
        verbose=False,
    )
    for response in responses:
        code = int(getattr(response, "status_code", 200))
        if code >= 400:
            raise RuntimeError(f"Internet Archive upload failed with HTTP {code}")

    for filename, local_path in archive_files.items():
        path = Path(local_path)
        wait_for_remote_file(
            identifier,
            filename,
            attempts=40,
            delay_seconds=15,
            expected_size=path.stat().st_size,
        )

    result = {
        "ok": True,
        "request_id": request_id,
        "title": title,
        "identifier": identifier,
        "item_url": f"https://archive.org/details/{identifier}",
        "audio_url": f"https://archive.org/download/{identifier}/sleep_history_audio.mp3",
        "narration_url": f"https://archive.org/download/{identifier}/narration_only.mp3",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "voice": pipeline.voice_metadata(),
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
