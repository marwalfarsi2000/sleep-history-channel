"""Targeted, resumable repair of one REVIEW_PENDING episode.

Quieter background noise and no black thumbnail card / artificial dark area. Never touches research,
outline, story, title, description or thumbnail hook text. The repair is a sequence of *units* (background,
thumbnail, one TTS chunk, narration, audio mix, video, publish). Each unit that completes is checkpointed
(see ``repair_checkpoint``) so a later workflow run on a fresh runner continues from the next missing unit.
Internet Archive stays untouched until every output is ready and has passed QC. Paid APIs are never used:
when the free Gemini TTS quota ends the run exits with REPAIR_WAITING_FOR_TTS_QUOTA.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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


import repair_checkpoint as cp  # noqa: E402

SESSION_DIR = ROOT / ".runtime" / "repair-checkpoint"
MANIFEST_PATH = SESSION_DIR / "manifest.json"
SESSION_PATH = SESSION_DIR / "session.json"
SNAPSHOT_DIR = ROOT / "repair-snapshot"
SMALL_DOWNLOADS = tuple(n for n in DOWNLOAD_NAMES if n not in ("14_video_final.mp4", "11_thumbnail_base.jpg"))
IMAGE_ATTEMPTS = 3
STATUS_WAITING = "REPAIR_WAITING_FOR_TTS_QUOTA"


class Waiting(RuntimeError):
    """A safe stop (free TTS quota used up); checkpointed work is preserved. Not a code defect."""


# --- thin media operations (patched in tests; no provider or ffmpeg needed there) -----------------------------

def validate_chunk_audio(path: Path) -> None:
    import pipeline
    if not path.is_file() or path.stat().st_size <= 1024:
        raise RuntimeError(f"AUDIO_QC_FAILED: empty chunk {path.name}")
    probe = pipeline.run([pipeline.ffmpeg(), "-v", "error", "-i", str(path), "-f", "null", "-"], check=False)
    if probe.returncode:
        raise RuntimeError(f"AUDIO_QC_FAILED: undecodable chunk {path.name}")


def synthesize_chunk(text: str, out: Path, model: str) -> str:
    import pipeline
    return asyncio.run(pipeline.tts_one(text, out, model))


def concat_narration(story: Path) -> None:
    import pipeline
    chunks = sorted((story / "audio_chunks").glob("*.mp3"))
    concat = story / "audio_chunks" / "concat.txt"
    concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in chunks), encoding="utf-8")
    pipeline.run([pipeline.ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c:a", "libmp3lame",
                  "-b:a", "160k", str(story / cp.NARRATION_FILE)])


def mix_and_qc(story: Path) -> None:
    import pipeline
    pipeline.mix_background_audio(story / cp.NARRATION_FILE, story / cp.MIXED_FILE)
    pipeline.run([pipeline.ffmpeg(), "-y", "-i", str(story / cp.MIXED_FILE), "-c:a", "libopus", "-b:a", "48k",
                  str(story / cp.OPUS_FILE)])
    pipeline.validate_audio(story)


def render_and_qc(story: Path) -> None:
    import pipeline
    from state_machine import State
    pipeline.background = lambda _story, force=False: None  # background handled by its own unit
    (story / cp.VIDEO_FILE).unlink(missing_ok=True)
    (story / "stage_manifest.json").unlink(missing_ok=True)
    pipeline.render(story)
    (story / "17_status.md").write_text(
        f"# الحالة\n\n- الحالة الحالية: `{State.GENERATING}`\n", encoding="utf-8")  # validate() needs it; package() rewrites
    pipeline.validate(story)
    pipeline.package(story)


def generate_background(story: Path) -> None:
    import image_generate
    for attempt in range(1, IMAGE_ATTEMPTS + 1):  # free providers only; a flat/low-quality candidate is re-rolled
        try:
            image_generate.generate_with_accuracy_retries(story)
            return
        except RuntimeError as exc:
            if "IMAGE_FREE_CAPACITY_EXHAUSTED" not in str(exc) or attempt == IMAGE_ATTEMPTS:
                raise
            print(f"Image attempt {attempt} unavailable ({str(exc)[:160]}); retrying in 30s", flush=True)
            time.sleep(30)


def render_thumbnail_file(story: Path, text: str) -> tuple[dict, str]:
    import image_generate
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    return image_generate.build_final_thumbnail(
        story,
        story / cp.BACKGROUND_FILE,
        text,
        str(metadata.get("thumbnail_prompt", "")).strip(),
    )


def inspect_video_frame(story: Path) -> dict:
    import pipeline
    from PIL import Image
    frame_path = story / "frame_inspect.png"
    pipeline.run([pipeline.ffmpeg(), "-y", "-ss", "10", "-i", str(story / cp.VIDEO_FILE), "-frames:v", "1", str(frame_path)])
    frame = Image.open(frame_path).convert("RGB")
    stats = analyse_frame(frame)
    print("VIDEO_FRAME_MAP\n" + "\n".join(stats["map"]), flush=True)
    return {"patch_in_video": stats["largest_cluster"] >= 8, "largest_cluster": stats["largest_cluster"],
            "frame_size": list(frame.size)}


def publish_to_archive(story: Path) -> dict:
    import archive_storage
    return archive_storage.upload_story(story)


def notify_review(story: Path, record: dict, identifier: str) -> None:
    import cloud_pipeline
    cloud_pipeline.review_record(story, record)
    cloud_pipeline.notify_existing_review(identifier)


def download_item_file(identifier: str, name: str, destination: Path) -> None:
    import cloud_pipeline
    cloud_pipeline.download_file(identifier, name, destination)


def item_metadata(identifier: str) -> dict:
    import archive_storage
    return archive_storage.archive_metadata(identifier)


# --- identity ---------------------------------------------------------------------------------------------------

def voice_identity() -> str:
    import pipeline
    meta = pipeline.voice_metadata()
    return cp.fingerprint(
        meta.get("voice_provider"), meta.get("voice_name"), cp.sha256_text(str(meta.get("voice_id", ""))),
        meta.get("tts_generation_speed"), meta.get("post_processing_tempo"), pipeline.TTS_AUDIO_CACHE_VERSION,
        pipeline.GEMINI_VOICE, int(pipeline.SETTINGS.get("gemini_tts_chunk_char_limit", 4200)))


def image_identity() -> str:
    import pipeline
    return cp.fingerprint(pipeline.SETTINGS.get("image_provider"), pipeline.SETTINGS.get("image_model"),
                          pipeline.SETTINGS.get("image_accuracy_retry_attempts"), "background-v1")


def thumbnail_identity(story: Path, text: str) -> str:
    import canva_thumbnail
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    scene_path = story / "08_image_prompt_en.txt"
    scene_prompt = scene_path.read_text(encoding="utf-8") if scene_path.exists() else ""
    effective_prompt = canva_thumbnail.build_background_prompt(
        str(metadata.get("thumbnail_prompt", "")).strip(),
        scene_prompt,
    )
    return canva_thumbnail.input_identity(text, effective_prompt)


def mix_identity() -> str:
    import pipeline
    noise = ROOT / str(pipeline.SETTINGS.get("background_audio_file", ""))
    return cp.fingerprint(round(float(pipeline.SETTINGS.get("background_audio_volume", 0.03)), 4),
                          pipeline.SETTINGS.get("background_audio_file"),
                          cp.sha256_file(noise) if noise.is_file() else "")


def render_identity() -> str:
    import pipeline
    return cp.fingerprint(" ".join(pipeline.video_codec()), "loop-r2-aac128-v1")


class Context:
    def __init__(self, identifier: str, story: Path, manifest: dict, chunk_texts: dict[int, str], thumbnail_text: str):
        self.identifier, self.story, self.manifest = identifier, story, manifest
        self.chunk_texts, self.thumbnail_text = chunk_texts, thumbnail_text

    def expected(self) -> dict:
        voice = self.manifest["tts_voice_identity"]
        return {
            "background_fp": cp.fingerprint(self.manifest["background_prompt_hash"], image_identity()),
            "thumbnail_identity": thumbnail_identity(self.story, self.thumbnail_text),
            "chunk_fps": {i: cp.fingerprint(cp.sha256_text(text), voice) for i, text in self.chunk_texts.items()},
            "total": len(self.chunk_texts),
            "mix_identity": mix_identity(),
            "render_identity": render_identity(),
        }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _session_key() -> str:
    return f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"


def _apply_metadata_patch(story: Path, manifest: dict) -> None:
    if not manifest.get("metadata_patch") or manifest.get("video_complete"):
        return  # a completed video already carries the final post-QC metadata
    path = story / "15_metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.update(manifest["metadata_patch"])
    _write_json(path, metadata)


def _write_chunk_models(story: Path, manifest: dict) -> None:
    models = {f"{int(i):03}.txt": e.get("model", "") for i, e in manifest["tts_chunks"].items()}
    _write_json(story / "logs" / "tts_chunk_models.json", models)


def bootstrap(identifier: str, store) -> Context:
    import pipeline
    session = {"key": _session_key(), "identifier": identifier}
    if MANIFEST_PATH.is_file() and SESSION_PATH.is_file():
        try:
            saved = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        except ValueError:
            saved = {}
        if saved.get("key") == session["key"] and saved.get("identifier") == identifier:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            story = ROOT / "stories" / manifest["episode_slug"]
            return _context_from_disk(identifier, story, manifest)
    remote = item_metadata(identifier)
    slug = str((remote.get("metadata") or {}).get("slug") or identifier)
    story = ROOT / "stories" / slug
    if story.exists():
        shutil.rmtree(story)
    story.mkdir(parents=True)
    available = {item.get("name") for item in remote.get("files", [])}
    for name in SMALL_DOWNLOADS:
        if name in available:
            download_item_file(identifier, name, story / name)
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    if cp_identifier(metadata) != identifier:
        raise RuntimeError("REPAIR_ABORTED: identifier does not match the episode metadata")
    tts_text = (story / "07_story_tts_api.md").read_text(encoding="utf-8").strip()
    tts_hash = cp.sha256_file(story / "07_story_tts_api.md")
    if tts_hash != metadata.get("tts_api_sha256"):
        raise RuntimeError("REPAIR_ABORTED: 07_story_tts_api.md differs from the reviewed episode")
    prompt_path = story / "08_image_prompt_en.txt"
    cleaned = sanitize_prompt(prompt_path.read_text(encoding="utf-8"))
    prompt_path.write_text(cleaned, encoding="utf-8")
    chunks = pipeline.split_chunks(tts_text, limit=int(pipeline.SETTINGS.get("gemini_tts_chunk_char_limit", 4200)))
    tdir = story / "tts_chunks"
    tdir.mkdir()
    for index, chunk in enumerate(chunks, 1):
        (tdir / f"{index:03}.txt").write_text(chunk + "\n", encoding="utf-8")
    manifest = cp.restore(store, slug, identifier, cp.repair_hash(slug, identifier, tts_hash), story) if store else None
    if manifest:
        say("REPAIR_CHECKPOINT_RESTORED", f"sequence={manifest.get('sequence')} chunks={cp.completed_chunks(manifest)}")
    else:
        manifest = cp.new_manifest(slug, identifier, tts_hash, voice_identity(), cp.sha256_text(cleaned.strip()), len(chunks))
        say("REPAIR_CHECKPOINT_RESTORED", "none (starting from scratch)")
    manifest["tts_voice_identity"] = voice_identity()
    manifest["background_prompt_hash"] = cp.sha256_text(cleaned.strip())
    context = Context(identifier, story, manifest, _texts(story), str(metadata.get("thumbnail_text", "")).strip())
    _settle(context)
    _write_json(SESSION_PATH, session)
    return context


def cp_identifier(metadata: dict) -> str:
    import archive_storage
    return archive_storage.safe_identifier(metadata["slug"], metadata.get("created_at", ""))


def _texts(story: Path) -> dict[int, str]:
    return {int(p.stem): p.read_text(encoding="utf-8") for p in sorted((story / "tts_chunks").glob("*.txt"))}


def _context_from_disk(identifier: str, story: Path, manifest: dict) -> Context:
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    context = Context(identifier, story, manifest, _texts(story), str(metadata.get("thumbnail_text", "")).strip())
    for line in cp.reconcile(manifest, story, context.expected()):
        say("REPAIR_CHECKPOINT_INVALIDATED", line)
    return context


def _settle(context: Context) -> None:
    for line in cp.reconcile(context.manifest, context.story, context.expected()):
        say("REPAIR_CHECKPOINT_INVALIDATED", line)
    _apply_metadata_patch(context.story, context.manifest)
    _write_chunk_models(context.story, context.manifest)
    _write_json(MANIFEST_PATH, context.manifest)


# --- units --------------------------------------------------------------------------------------------------------

def unit_background(c: Context, name: str) -> list[str]:
    story, manifest = c.story, c.manifest
    video = story / cp.VIDEO_FILE
    if not video.is_file():
        download_item_file(c.identifier, cp.VIDEO_FILE, video)
    decision = inspect_video_frame(story)
    manifest["decision"] = decision
    say("VIDEO_FRAME_INSPECTED", "YES")
    say("DARK_PATCH_VISIBLE_IN_CURRENT_VIDEO", f"{'YES' if decision['patch_in_video'] else 'NO'} (cluster {decision['largest_cluster']})")
    background = story / cp.BACKGROUND_FILE
    if decision["patch_in_video"]:
        background.unlink(missing_ok=True)
        generate_background(story)
        say("BACKGROUND_REGENERATED", "YES")
    else:
        shutil.copy2(story / "frame_inspect.png", background)
        say("BACKGROUND_REGENERATED", "NO (reused the clean frame of the reviewed video)")
    if not background.is_file():
        raise RuntimeError("REPAIR_FAILED: background was not produced")
    prompt_text = (story / "08_image_prompt_en.txt").read_text(encoding="utf-8").strip()
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    metadata.update(image_sha256=cp.sha256_file(background), image_prompt_sha256=cp.sha256_text(prompt_text))
    _write_json(story / "15_metadata.json", metadata)
    manifest["metadata_patch"] = {k: v for k, v in metadata.items() if k.startswith(("image_", "thumbnail_"))}
    video.unlink(missing_ok=True)
    (story / "frame_inspect.png").unlink(missing_ok=True)
    manifest.update(background_complete=True, background_sha256=cp.sha256_file(background),
                    background_fp=c.expected()["background_fp"], background_artifact=name)
    return [cp.BACKGROUND_FILE]


def unit_thumbnail(c: Context, name: str) -> list[str]:
    story, manifest = c.story, c.manifest
    thumbnail = story / cp.THUMBNAIL_FILE
    metadata = json.loads((story / "15_metadata.json").read_text(encoding="utf-8"))
    expected_identity = c.expected()["thumbnail_identity"]
    sha = cp.sha256_file(thumbnail) if thumbnail.is_file() else ""
    reusable = (
        thumbnail.is_file()
        and metadata.get("thumbnail_generation_provider") == "canva_via_n8n"
        and metadata.get("thumbnail_text_render_status") == "passed"
        and metadata.get("thumbnail_input_sha256") == expected_identity
        and metadata.get("thumbnail_sha256") == sha
    )

    if reusable:
        thumbnail_record = {
            key: value for key, value in metadata.items() if key.startswith("thumbnail_")
        }
        thumbnail_status = "passed"
        say("THUMBNAIL_REBUILT", "NO (reused current Canva thumbnail)")
    else:
        thumbnail_record, thumbnail_status = render_thumbnail_file(story, c.thumbnail_text)
        sha = cp.sha256_file(thumbnail)
        say("THUMBNAIL_REBUILT", "YES")

    patch = manifest.setdefault("metadata_patch", {})
    patch.update(thumbnail_record)
    patch.update(
        thumbnail_sha256=sha,
        thumbnail_input_sha256=expected_identity,
        thumbnail_text_render_status=thumbnail_status,
        thumbnail_path=str(thumbnail),
    )
    _apply_metadata_patch(story, manifest)
    manifest.update(
        thumbnail_complete=True,
        thumbnail_sha256=sha,
        thumbnail_artifact=name,
        thumbnail_fp=cp.fingerprint(manifest["background_sha256"], expected_identity),
    )
    return [cp.THUMBNAIL_FILE]


def unit_chunk(c: Context, index: int, name: str) -> list[str]:
    import pipeline
    story, manifest = c.story, c.manifest
    pipeline.clear_expired_gemini_quota_state()
    if pipeline.gemini_quota_exhausted_today():
        raise Waiting("Gemini free TTS quota is already marked exhausted for this quota day")
    (story / "audio_chunks").mkdir(exist_ok=True)
    out = story / cp.chunk_file(index)
    out.unlink(missing_ok=True)
    text = c.chunk_texts[index]
    model = ""
    for candidate in pipeline._gemini_tts_models():
        try:
            model = synthesize_chunk(text, out, candidate) or candidate
            break
        except pipeline.TtsEngineExhausted as exc:
            print(f"{exc}; trying the next Gemini model (same narrator)", flush=True)
    if not model:
        pipeline.mark_gemini_quota_exhausted("repair run: every Gemini TTS model is out of quota")
        raise Waiting("every Gemini TTS model is out of free quota")
    validate_chunk_audio(out)  # a chunk is never recorded before it validates
    manifest["tts_chunks"][str(index)] = {
        "complete": True, "text_sha256": cp.sha256_text(text), "provider": "gemini", "model": model,
        "file": cp.chunk_file(index), "sha256": cp.sha256_file(out),
        "fp": c.expected()["chunk_fps"][index], "artifact": name,
    }
    _write_chunk_models(story, manifest)
    say("REPAIR_TTS_CHUNK_GENERATED", index)
    return [cp.chunk_file(index)]


def unit_narration(c: Context, name: str) -> list[str]:
    concat_narration(c.story)
    path = c.story / cp.NARRATION_FILE
    if not path.is_file() or path.stat().st_size <= 100_000:
        raise RuntimeError("AUDIO_QC_FAILED: narration assembly produced no usable file")
    c.manifest.update(narration_complete=True, narration_sha256=cp.sha256_file(path), narration_artifact=name,
                      narration_fp=cp.narration_fingerprint(c.manifest))
    return [cp.NARRATION_FILE]


def unit_mixed(c: Context, name: str) -> list[str]:
    mix_and_qc(c.story)
    manifest = c.manifest
    manifest.update(mixed_audio_complete=True, mixed_audio_sha256=cp.sha256_file(c.story / cp.MIXED_FILE),
                    review_opus_sha256=cp.sha256_file(c.story / cp.OPUS_FILE), mixed_audio_artifact=name,
                    mixed_audio_fp=cp.fingerprint(manifest["narration_sha256"], c.expected()["mix_identity"]))
    import pipeline
    say("AUDIO_REMIXED", f"YES at {pipeline.background_volume_percent():g}%")
    return [cp.MIXED_FILE, cp.OPUS_FILE]


VIDEO_EXTRAS = ("15_metadata.json", "00_package.html", "17_status.md")


def unit_video(c: Context, name: str) -> list[str]:
    render_and_qc(c.story)
    manifest = c.manifest
    extras = {rel: cp.sha256_file(c.story / rel) for rel in VIDEO_EXTRAS if (c.story / rel).is_file()}
    manifest.update(video_complete=True, video_sha256=cp.sha256_file(c.story / cp.VIDEO_FILE), video_artifact=name,
                    video_extra=extras,
                    video_fp=cp.fingerprint(manifest["background_sha256"], manifest["mixed_audio_sha256"],
                                            c.expected()["render_identity"]))
    say("VIDEO_RERENDERED", "YES")
    return [cp.VIDEO_FILE, *extras]


def unit_publish(c: Context, name: str) -> list[str]:
    manifest = c.manifest
    if not manifest["archive_uploaded"]:
        record = publish_to_archive(c.story)
        if record["identifier"] != c.identifier:
            raise RuntimeError("REPAIR_ABORTED: archive identifier changed")
        manifest["archive_uploaded"] = True
        manifest["archive_record"] = {k: record.get(k) for k in ("identifier", "video_size", "video_sha256")}
        say("REMOTE_HASH_VERIFICATION", f"PASSED size={record.get('video_size')} sha256={record.get('video_sha256')}")
        _write_json(MANIFEST_PATH, manifest)
        notify_review(c.story, record, c.identifier)
    else:
        import cloud_pipeline
        cloud_pipeline.notify_existing_review(c.identifier)
    manifest["telegram_notified"] = True
    say("TELEGRAM_REVIEW_RESENT", "YES")
    say("STATUS_STILL_REVIEW_PENDING", "YES")
    return []


# --- orchestration ------------------------------------------------------------------------------------------------

def emit_outputs(**values: object) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(lines)
    print(lines, end="", flush=True)


def run_unit(identifier: str, dry_run: bool = False, store=None) -> dict:
    """Perform exactly the next missing unit, checkpoint it, and say whether more work remains."""
    if SNAPSHOT_DIR.exists():
        shutil.rmtree(SNAPSHOT_DIR)
    context = bootstrap(identifier, store)
    manifest = context.manifest
    unit = cp.next_unit(manifest, dry_run)
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    if unit == "done":
        say("REPAIR_STATUS", "COMPLETE")
        emit_outputs(more="false", snapshot="", status="COMPLETE")
        return {"status": "COMPLETE"}
    sequence = manifest["sequence"] + 1
    name = cp.snapshot_name(manifest["episode_slug"], manifest["repair_hash"], run_id + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1"), sequence)
    try:
        if unit == "background":
            files = unit_background(context, name)
        elif unit == "thumbnail":
            files = unit_thumbnail(context, name)
        elif unit.startswith("chunk:"):
            files = unit_chunk(context, int(unit.split(":")[1]), name)
        elif unit == "narration":
            files = unit_narration(context, name)
        elif unit == "mixed":
            files = unit_mixed(context, name)
        elif unit == "video":
            files = unit_video(context, name)
        else:
            files = unit_publish(context, name)
    except Waiting as exc:
        done = cp.completed_chunks(manifest)
        manifest["waiting_reason"] = str(exc)
        message = (f"{STATUS_WAITING} completed_chunks={len(done)} total_chunks={manifest['tts_chunks_total']} "
                   f"next_chunk={next((i for i in range(1, manifest['tts_chunks_total'] + 1) if str(i) not in manifest['tts_chunks']), 0)}")
        say("REPAIR_STATUS", message)
        print(f"::warning::{message}")
        cp.save_index(manifest["episode_slug"], cp.index_payload(manifest, "", run_id))
        emit_outputs(more="false", snapshot="", status=STATUS_WAITING)
        return {"status": STATUS_WAITING, "message": message}
    manifest["sequence"] = sequence
    manifest["waiting_reason"] = None
    _write_json(MANIFEST_PATH, manifest)
    cp.write_snapshot(manifest, context.story, files, SNAPSHOT_DIR)
    cp.save_index(manifest["episode_slug"], cp.index_payload(manifest, name, run_id))
    say("REPAIR_CHECKPOINT_SAVED", f"{name} unit={unit}")
    more = cp.next_unit(manifest, dry_run) != "done"
    emit_outputs(more=str(more).lower(), snapshot=name, status="CONTINUE" if more else "COMPLETE")
    return {"status": "CONTINUE" if more else "COMPLETE", "unit": unit, "snapshot": name}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identifier", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    store = cp.GhArtifactStore() if os.environ.get("GITHUB_REPOSITORY") else None
    result = run_unit(args.identifier, args.dry_run, store)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
