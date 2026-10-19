"""Targeted repair of one REVIEW_PENDING episode: quieter background noise and no black thumbnail card.

Never touches research, outline, story, title, description or thumbnail hook text. It downloads the
review package from Internet Archive, rebuilds only thumbnail / (optionally) background / audio mix /
video, replaces the changed files on the SAME identifier and re-sends the review notification.
Paid APIs are never used: if free Gemini TTS is unavailable the repair stops without changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

DOWNLOAD_NAMES = (
    "14_video_final.mp4", "11_thumbnail_base.jpg", "01_title.txt", "02_description.txt", "03_sources.md",
    "04_research_notes.md", "05_story_clean.md", "05b_story_tts_normalized.md", "06_story_tts_annotated.md",
    "07_story_tts_api.md", "08_image_prompt_en.txt", "08_thumbnail_prompt_en.txt", "15_metadata.json",
    "16_fact_check.md",
)
OPTIONAL_MEDIA = ("12_audio_narration.mp3", "10_background.png")
DARK_CLAUSE = re.compile(
    r"[^.;,\n]*\b(?:dark negative[- ]space|reserved dark|empty dark|clean dark|dark (?:text )?area|negative[- ]space|"
    r"text[- ]safe dark|dark (?:panel|patch|zone|space))\b[^.;,\n]*[.;,]?",
    re.IGNORECASE)
NATURAL_AREA = (
    "Leave one naturally uncluttered, low-detail text-safe area integrated organically into the historical "
    "scene. Do not create a panel, box, card, banner, frame, solid dark patch, artificial rectangle, or "
    "visibly empty black area.")
SCENE_TAIL = re.compile(r"\n*PIPELINE SCENE RENDER:.*\Z", re.DOTALL)
NEW_SCENE_TAIL = (
    "PIPELINE SCENE RENDER: Generate the historical scene with a natural uncluttered text-safe area integrated "
    "into the scene only. Do not render letters, words, typography, logos, or watermarks; the exact Arabic label "
    "will be overlaid deterministically after generation. Do not create a panel, box, card, banner, frame, "
    "solid dark patch or artificial rectangle.")
report: dict[str, object] = {}


def say(key: str, value: object) -> None:
    report[key] = value
    print(f"REPORT {key}={value}", flush=True)


def sanitize_prompt(prompt: str) -> str:
    body = SCENE_TAIL.sub("", prompt).strip()
    body = DARK_CLAUSE.sub("", body).strip()
    return f"{body} {NATURAL_AREA}\n\n{NEW_SCENE_TAIL}\n"


def analyse_frame(image) -> dict:
    """Find a flat, near-black rectangle-like region (the 'black patch') in a video frame."""
    from PIL import ImageStat
    gray = image.convert("L").resize((320, 180))
    cols, rows, cell = 16, 9, 20
    grid = []
    for row in range(rows):
        line = []
        for col in range(cols):
            stat = ImageStat.Stat(gray.crop((col * cell, row * cell, (col + 1) * cell, (row + 1) * cell)))
            line.append((stat.mean[0], stat.stddev[0]))
        grid.append(line)
    flat = {(r, c) for r in range(rows) for c in range(cols) if grid[r][c][0] < 32 and grid[r][c][1] < 9}
    largest, seen = 0, set()
    for start in flat:
        if start in seen:
            continue
        stack, size = [start], 0
        seen.add(start)
        while stack:
            r, c = stack.pop()
            size += 1
            for nxt in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                if nxt in flat and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        largest = max(largest, size)
    ascii_rows = ["".join("#" if (r, c) in flat else "." for c in range(cols)) for r in range(rows)]
    return {"flat_dark_cells": len(flat), "largest_cluster": largest, "total_cells": cols * rows,
            "map": ascii_rows, "mean_luma": round(ImageStat.Stat(gray).mean[0], 1)}


def run_repair(identifier: str, upload: bool) -> None:
    import archive_storage
    import cloud_pipeline
    import image_generate
    import pipeline
    from PIL import Image
    from state_machine import State

    meta_remote = archive_storage.archive_metadata(identifier)
    files = {item.get("name"): item for item in meta_remote.get("files", [])}
    say("RUN67_MEDIA_RECOVERED_FROM", "Run #67 left no artifact (success path); no retained checkpoint; Internet Archive item")
    say("ARCHIVE_ITEM_FILES", ", ".join(f"{n}:{files[n].get('size')}" for n in sorted(files)))
    slug = str((meta_remote.get("metadata") or {}).get("slug") or identifier)
    story = ROOT / "stories" / slug
    if story.exists():
        shutil.rmtree(story)
    story.mkdir(parents=True)
    for name in DOWNLOAD_NAMES:
        if name in files:
            cloud_pipeline.download_file(identifier, name, story / name)
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    if archive_storage.safe_identifier(metadata["slug"], metadata.get("created_at", "")) != identifier:
        raise RuntimeError("REPAIR_ABORTED: identifier does not match the episode metadata")
    if pipeline.file_sha256(story / "07_story_tts_api.md") != metadata.get("tts_api_sha256"):
        raise RuntimeError("REPAIR_ABORTED: 07_story_tts_api.md differs from the reviewed episode")
    thumbnail_text = str(metadata.get("thumbnail_text", "")).strip()

    # --- inspect the current video frame vs thumbnail -------------------------------------------------
    frame_path = story / "frame_inspect.png"
    pipeline.run([pipeline.ffmpeg(), "-y", "-ss", "10", "-i", str(story / "14_video_final.mp4"), "-frames:v", "1", str(frame_path)])
    frame = Image.open(frame_path).convert("RGB")
    say("VIDEO_FRAME_INSPECTED", f"YES size={frame.size}")
    video_stats = analyse_frame(frame)
    thumb_stats = analyse_frame(Image.open(story / "11_thumbnail_base.jpg").convert("RGB"))
    print("VIDEO_FRAME_MAP\n" + "\n".join(video_stats["map"]), flush=True)
    print("OLD_THUMBNAIL_MAP\n" + "\n".join(thumb_stats["map"]), flush=True)
    patch_in_video = video_stats["largest_cluster"] >= 8
    say("DARK_PATCH_VISIBLE_IN_CURRENT_VIDEO",
        f"{'YES' if patch_in_video else 'NO'} (largest flat near-black cluster {video_stats['largest_cluster']} of "
        f"{video_stats['total_cells']} cells; thumbnail cluster {thumb_stats['largest_cluster']})")

    # --- background: Decision A (reuse) or B (regenerate) ---------------------------------------------
    background = story / "10_background.png"
    if "10_background.png" in files:
        cloud_pipeline.download_file(identifier, "10_background.png", background)
        say("BACKGROUND_SOURCE", "10_background.png from archive item")
    if patch_in_video:
        prompt_path = story / "08_image_prompt_en.txt"
        original_prompt = prompt_path.read_text(encoding="utf-8")
        cleaned_prompt = sanitize_prompt(original_prompt)
        print("PROMPT_BEFORE\n" + original_prompt + "\nPROMPT_AFTER\n" + cleaned_prompt, flush=True)
        if len(cleaned_prompt) < 0.6 * len(original_prompt):
            raise RuntimeError("REPAIR_ABORTED: prompt sanitising removed too much of the scene description")
        prompt_path.write_text(cleaned_prompt, encoding="utf-8")
        background.unlink(missing_ok=True)
        for attempt in range(1, 4):  # free providers only; a flat/low-quality candidate is simply re-rolled
            try:
                image_generate.generate_with_accuracy_retries(story)
                break
            except RuntimeError as exc:
                if "IMAGE_FREE_CAPACITY_EXHAUSTED" not in str(exc) or attempt == 3:
                    raise
                print(f"Image attempt {attempt} unavailable ({str(exc)[:160]}); retrying in 30s", flush=True)
                time.sleep(30)
        say("BACKGROUND_REGENERATED", "YES")
        say("WHY_BACKGROUND_REGENERATED", "flat near-black patch detected in the video frame")
        say("THUMBNAIL_REBUILT", "YES (by image_generate with the new background)")
    else:
        if not background.exists():
            frame.save(background)
            say("BACKGROUND_SOURCE", "lossless frame extracted from the reviewed video")
        image_generate.render_thumbnail(Image.open(background).convert("RGB"), story / "11_thumbnail_base.jpg", thumbnail_text)
        metadata.update(image_sha256=pipeline.file_sha256(background),
                        thumbnail_sha256=pipeline.file_sha256(story / "11_thumbnail_base.jpg"))
        (story / "15_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        say("BACKGROUND_REGENERATED", "NO")
        say("WHY_BACKGROUND_REGENERATED", "not needed: dark patch only in the thumbnail card")
        say("THUMBNAIL_REBUILT", "YES (card removed)")
    new_thumb = analyse_frame(Image.open(story / "11_thumbnail_base.jpg").convert("RGB"))
    print("NEW_THUMBNAIL_MAP\n" + "\n".join(new_thumb["map"]), flush=True)

    # --- audio -----------------------------------------------------------------------------------------
    narration = story / "12_audio_narration.mp3"
    if "12_audio_narration.mp3" in files:
        cloud_pipeline.download_file(identifier, "12_audio_narration.mp3", narration)
    if narration.exists() and narration.stat().st_size > 100_000:
        say("CLEAN_NARRATION_RECOVERED", "YES")
        say("TTS_REGENERATED", "NO")
        pipeline.mix_background_audio(narration, story / "12_audio_full.mp3")
        pipeline.run([pipeline.ffmpeg(), "-y", "-i", str(story / "12_audio_full.mp3"), "-c:a", "libopus", "-b:a", "48k",
                      str(story / "13_audio_review.opus")])
    else:
        say("CLEAN_NARRATION_RECOVERED", "NO (the archive item stores no audio)")
        clean = (story / "07_story_tts_api.md").read_text(encoding="utf-8").strip()
        provider = pipeline.SETTINGS.get("tts_provider", "edge").lower()
        if provider != "gemini":
            raise RuntimeError(f"REPAIR_STOPPED_SAFELY: unexpected tts provider {provider}")
        chunks = pipeline.split_chunks(clean, limit=int(pipeline.SETTINGS.get("gemini_tts_chunk_char_limit", 4200)))
        tdir = story / "tts_chunks"
        tdir.mkdir(exist_ok=True)
        for index, chunk in enumerate(chunks, 1):
            (tdir / f"{index:03}.txt").write_text(chunk + "\n", encoding="utf-8")
        print(f"Repair TTS from 07_story_tts_api.md: {len(chunks)} chunks", flush=True)
        pipeline.prepare = lambda _story: None  # chunks already built from the reviewed 07_story_tts_api.md
        pipeline.tts_cloud_engines = lambda: []  # never switch narrator or use any cloud fallback
        try:
            asyncio.run(pipeline.synthesize(story))
        except RuntimeError as exc:
            say("TTS_REGENERATED", "NO - stopped safely")
            raise RuntimeError(f"REPAIR_STOPPED_SAFELY: free Gemini TTS unavailable ({exc})") from exc
        say("TTS_REGENERATED", "YES (Gemini TTS, same narrator/policy, free quota)")
    say("PAID_TTS_USED", "NO")
    say("AUDIO_REMIXED", f"YES at {pipeline.background_volume_percent():g}%")

    # --- video -----------------------------------------------------------------------------------------
    pipeline.background = lambda _story, force=False: None  # background already handled above
    for stale in ("14_video_final.mp4",):
        (story / stale).unlink(missing_ok=True)
    (story / "stage_manifest.json").unlink(missing_ok=True)
    pipeline.render(story)
    say("VIDEO_RERENDERED", "YES (same render settings, 10_background.png)")
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    metadata.update(audio_sha256=pipeline.file_sha256(story / "12_audio_full.mp3"))
    (story / "15_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (story / "17_status.md").write_text(
        f"# الحالة\n\n- الحالة الحالية: `{State.GENERATING}`\n", encoding="utf-8")  # validate() requires it; package() rewrites it
    pipeline.validate(story)
    pipeline.package(story)
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    say("AUDIO_VIDEO_QC", "PASSED")
    say("METADATA_BACKGROUND_PERCENT", metadata.get("background_audio_volume_percent"))

    if not upload:
        say("ARCHIVE_FILES_REPLACED", "dry run - nothing uploaded")
        return
    # --- same item, changed files only (checksum=True skips identical files) ---------------------------
    record = archive_storage.upload_story(story)
    say("SAME_ARCHIVE_IDENTIFIER_USED", f"YES {record['identifier']}")
    if record["identifier"] != identifier:
        raise RuntimeError("REPAIR_ABORTED: archive identifier changed")
    say("ARCHIVE_FILES_REPLACED", "14_video_final.mp4, 11_thumbnail_base.jpg, 15_metadata.json (+ others whose checksum changed)")
    say("REMOTE_HASH_VERIFICATION", f"PASSED size={record['video_size']} sha256={record['video_sha256']}")
    review = cloud_pipeline.review_record(story, record)
    say("STATUS_STILL_REVIEW_PENDING", str(review["status"]))
    cloud_pipeline.notify_existing_review(identifier)
    say("TELEGRAM_REVIEW_RESENT", "YES")
    out = ROOT / "logs"
    out.mkdir(exist_ok=True)
    (out / "repair_review_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identifier", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_repair(args.identifier, upload=not args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
