"""Durable, resumable repair checkpoint: chunks and background must survive across separate runners."""
import json
import os
import shutil
import sys
from pathlib import Path

import re

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import archive_storage  # noqa: E402
import pipeline  # noqa: E402
import repair_checkpoint as cp  # noqa: E402
import repair_review as rr  # noqa: E402

SLUG = "tripoli-test-episode"
CHUNKS = 11
CREATED = "2026-10-19T08:00:00+00:00"
IDENTIFIER = archive_storage.safe_identifier(SLUG, CREATED)
TTS_TEXT = "\n\n".join(f"chunk text {i}" for i in range(1, CHUNKS + 1))


class Fake:
    """Replaces every provider / ffmpeg / network operation; counts the expensive calls."""

    def __init__(self, tmp: Path, monkeypatch):
        self.tmp, self.mp = tmp, monkeypatch
        self.tts_calls: list[int] = []
        self.image_calls = 0
        self.publish_calls = 0
        self.notify_calls = 0
        self.fail_after_chunks: int | None = None  # simulate quota exhaustion after N successful chunks
        self.chunk_texts = [f"chunk text {i}\n" for i in range(1, CHUNKS + 1)]
        self.prompt = "A lantern lit alley in Tripoli, clean dark negative-space area for text. 16:9."
        self.volume_token = "vol-0.03"
        self.voice = "voice-A"
        self.successes_this_run = 0
        self.indexes: list[dict] = []
        self.marked_exhausted = False
        self.store = cp.DirArtifactStore(tmp / "artifacts")
        self.root = tmp / "repo"
        (self.root / ".runtime").mkdir(parents=True)
        self.install()

    def metadata(self) -> dict:
        return {"slug": SLUG, "created_at": CREATED, "title": "T", "thumbnail_text": "أزقة طرابلس البيضاء",
                "tts_api_sha256": cp.sha256_text(TTS_TEXT + "\n")}

    def install(self):
        mp, root = self.mp, self.root
        mp.setattr(rr, "ROOT", root)
        mp.setattr(rr, "SESSION_DIR", root / ".runtime" / "repair-checkpoint")
        mp.setattr(rr, "MANIFEST_PATH", root / ".runtime" / "repair-checkpoint" / "manifest.json")
        mp.setattr(rr, "SESSION_PATH", root / ".runtime" / "repair-checkpoint" / "session.json")
        mp.setattr(rr, "SNAPSHOT_DIR", root / "repair-snapshot")
        mp.setenv("GITHUB_ACTIONS", "true")
        mp.setenv("GEMINI_API_KEY", "SECRET-GEMINI-KEY")
        mp.setenv("IAS3_SECRET_KEY", "SECRET-IA-KEY")
        mp.setattr(rr, "item_metadata", lambda ident: {
            "metadata": {"slug": SLUG}, "files": [{"name": n} for n in rr.DOWNLOAD_NAMES]})

        def download(ident, name, destination: Path):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if name == "15_metadata.json":
                destination.write_text(json.dumps(self.metadata()), encoding="utf-8")
            elif name == "07_story_tts_api.md":
                destination.write_text(TTS_TEXT + "\n", encoding="utf-8")
            elif name == "08_image_prompt_en.txt":
                destination.write_text(self.prompt, encoding="utf-8")
            else:
                destination.write_bytes(b"old-" + name.encode())
        mp.setattr(rr, "download_item_file", download)
        mp.setattr(pipeline, "split_chunks", lambda text, limit=0: [c.strip() for c in self.chunk_texts])
        mp.setattr(rr, "voice_identity", lambda: self.voice)
        mp.setattr(rr, "mix_identity", lambda: self.volume_token)
        mp.setattr(rr, "render_identity", lambda: "render-1")
        mp.setattr(rr, "inspect_video_frame",
                   lambda story: (shutil.copy(__file__, story / "frame_inspect.png"),
                                  {"patch_in_video": True, "largest_cluster": 12, "frame_size": [1920, 1080]})[1])
        mp.setattr(rr, "sanitize_prompt", lambda text: self.prompt.replace("clean dark negative-space area", "natural area"))

        def background(story: Path):
            self.image_calls += 1
            (story / cp.BACKGROUND_FILE).write_bytes(b"BG-%d" % self.image_calls)
        mp.setattr(rr, "generate_background", background)
        mp.setattr(rr, "render_thumbnail_file",
                   lambda story, text: (story / cp.THUMBNAIL_FILE).write_bytes(b"THUMB-" + (story / cp.BACKGROUND_FILE).read_bytes()))
        mp.setattr(pipeline, "clear_expired_gemini_quota_state", lambda: None)
        mp.setattr(pipeline, "gemini_quota_exhausted_today", lambda: self.marked_exhausted)
        mp.setattr(pipeline, "mark_gemini_quota_exhausted", lambda reason: setattr(self, "marked_exhausted", True))
        mp.setattr(pipeline, "_gemini_tts_models", lambda: ["m1", "m2"])

        def synth(text: str, out: Path, model: str) -> str:
            index = int(text.split()[-1])
            if self.fail_after_chunks is not None and self.successes_this_run >= self.fail_after_chunks:
                raise pipeline.TtsEngineExhausted("gemini", "daily free quota exhausted")
            self.tts_calls.append(index)
            self.successes_this_run += 1
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(f"audio-{index}-".encode() * 200)
            return model
        mp.setattr(rr, "synthesize_chunk", synth)
        mp.setattr(rr, "validate_chunk_audio", lambda path: None if path.stat().st_size > 1024 else (_ for _ in ()).throw(RuntimeError("bad")))
        mp.setattr(rr, "concat_narration", lambda story: (story / cp.NARRATION_FILE).write_bytes(
            b"".join(p.read_bytes() for p in sorted((story / "audio_chunks").glob("*.mp3"))) + b"N" * 120_000))
        mp.setattr(rr, "mix_and_qc", lambda story: ((story / cp.MIXED_FILE).write_bytes(
            (story / cp.NARRATION_FILE).read_bytes() + self.volume_token.encode()),
            (story / cp.OPUS_FILE).write_bytes(b"opus-" + self.volume_token.encode())))

        def render(story: Path):
            (story / cp.VIDEO_FILE).write_bytes((story / cp.BACKGROUND_FILE).read_bytes() + (story / cp.MIXED_FILE).read_bytes())
            (story / "00_package.html").write_text("<html>", encoding="utf-8")
            (story / "17_status.md").write_text("status", encoding="utf-8")
        mp.setattr(rr, "render_and_qc", render)

        def publish(story: Path):
            manifest = json.loads(rr.MANIFEST_PATH.read_text(encoding="utf-8"))
            assert manifest["video_complete"] and (story / cp.VIDEO_FILE).is_file(), "partial upload"
            assert manifest["background_complete"] and manifest["narration_complete"] and manifest["mixed_audio_complete"]
            self.publish_calls += 1
            return {"identifier": IDENTIFIER, "video_size": 1, "video_sha256": "x"}
        mp.setattr(rr, "publish_to_archive", publish)

        def notify(story, record, ident):
            self.notify_calls += 1
        mp.setattr(rr, "notify_review", notify)
        import cloud_pipeline
        mp.setattr(cloud_pipeline, "notify_existing_review", lambda ident: setattr(self, "notify_calls", self.notify_calls + 1))
        mp.setattr(cp, "save_index", lambda slug, payload: self.indexes.append(payload) or True)

    def new_runner(self, run_id: str):
        """A completely fresh GitHub-hosted runner: no story dir, no session state, only artifacts persist."""
        shutil.rmtree(self.root / "stories", ignore_errors=True)
        shutil.rmtree(self.root / ".runtime", ignore_errors=True)
        shutil.rmtree(self.root / "repair-snapshot", ignore_errors=True)
        (self.root / ".runtime").mkdir(parents=True)
        self.mp.setenv("GITHUB_RUN_ID", run_id)
        self.mp.setenv("GITHUB_RUN_ATTEMPT", "1")
        self.successes_this_run = 0
        self.tts_calls = []
        self.image_calls = 0
        self.publish_calls = 0
        self.notify_calls = 0

    def run(self, run_id: str, *, dry_run: bool = False, fail_after_chunks: int | None = None, fresh: bool = True) -> list[dict]:
        if fresh:
            self.new_runner(run_id)
        self.fail_after_chunks = fail_after_chunks
        results = []
        for _ in range(40):
            result = rr.run_unit(IDENTIFIER, dry_run, self.store)
            snapshot = rr.SNAPSHOT_DIR
            if result.get("snapshot"):
                self.store.publish(result["snapshot"], snapshot)  # the workflow's upload-artifact step
            results.append(result)
            if result["status"] != "CONTINUE":
                break
        return results

    def manifest(self) -> dict:
        return json.loads(rr.MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Fake(tmp_path, monkeypatch)


def test_run_a_checkpoints_chunks_1_to_4_then_waits_without_error(env):
    results = env.run("100", fail_after_chunks=4)
    assert results[-1]["status"] == rr.STATUS_WAITING
    assert "completed_chunks=4 total_chunks=11 next_chunk=5" in results[-1]["message"]
    assert env.tts_calls == [1, 2, 3, 4]
    assert cp.completed_chunks(env.manifest()) == [1, 2, 3, 4]
    assert env.manifest()["background_complete"] and env.manifest()["thumbnail_complete"]
    assert env.publish_calls == 0 and env.notify_calls == 0  # nothing partial reaches Internet Archive / Telegram
    assert env.marked_exhausted
    assert env.indexes[-1]["completed_tts_chunks"] == [1, 2, 3, 4]
    assert env.indexes[-1]["waiting_reason"]


def test_every_chunk_gets_its_own_durable_snapshot(env):
    env.run("100", fail_after_chunks=4)
    names = sorted(p.name for p in (env.tmp / "artifacts").iterdir())
    chunk_snaps = [n for n in names if (env.tmp / "artifacts" / n / "audio_chunks").exists()]
    assert len(chunk_snaps) == 4
    for name in chunk_snaps:
        assert len(list((env.tmp / "artifacts" / name / "audio_chunks").iterdir())) == 1  # incremental, not duplicated
        assert (env.tmp / "artifacts" / name / "manifest.json").is_file()


def test_run_b_restores_chunks_and_background_and_continues_at_chunk_5(env):
    env.run("100", fail_after_chunks=4)
    env.marked_exhausted = False
    results = env.run("200")
    assert results[-1]["status"] == "COMPLETE"
    assert env.tts_calls == [5, 6, 7, 8, 9, 10, 11]  # Gemini is never called for 1-4
    assert env.image_calls == 0  # Cloudflare is not called again
    assert env.publish_calls == 1 and env.notify_calls == 1


def test_corrupt_restored_chunk_regenerates_only_that_chunk(env):
    env.run("100", fail_after_chunks=4)
    env.marked_exhausted = False
    corrupt = next((env.tmp / "artifacts").glob("*/audio_chunks/003.mp3"))
    corrupt.write_bytes(b"tampered" * 500)
    env.run("200", dry_run=True)
    assert env.tts_calls == [3, 5, 6, 7, 8, 9, 10, 11]
    assert env.image_calls == 0


def test_corrupt_background_regenerates_background_and_video_only(env):
    env.run("100", dry_run=True)
    next((env.tmp / "artifacts").glob("*/10_background.png")).write_bytes(b"tampered")
    env.run("200", dry_run=True)
    assert env.image_calls == 1 and env.tts_calls == []


def test_changed_chunk_text_invalidates_that_chunk_and_everything_downstream(env):
    env.run("100", dry_run=True)
    env.chunk_texts[2] = "chunk text 3 edited\n"
    env.new_runner("200")
    manifest = env.manifest() if rr.MANIFEST_PATH.exists() else None
    assert manifest is None
    env.mp.setattr(rr, "synthesize_chunk", env_synth(env))
    env.run("200", dry_run=True, fresh=False)
    assert env.tts_calls == [3]


def env_synth(env):
    def synth(text, out, model):
        env.tts_calls.append(int(text.split()[2]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"audio-3-edited" * 200)
        return model
    return synth


def test_changed_voice_identity_invalidates_all_tts(env):
    env.run("100", dry_run=True)
    env.voice = "voice-B"
    env.run("200", dry_run=True)
    assert env.tts_calls == list(range(1, CHUNKS + 1))
    assert env.image_calls == 0


def test_changed_pink_noise_volume_keeps_narration_and_rebuilds_mix_and_video(env):
    env.run("100", dry_run=True)
    env.volume_token = "vol-0.05"
    env.run("200", dry_run=True)
    manifest = env.manifest()
    assert env.tts_calls == [] and env.image_calls == 0
    assert manifest["mixed_audio_complete"] and manifest["video_complete"]
    assert (env.root / "stories" / SLUG / cp.MIXED_FILE).read_bytes().endswith(b"vol-0.05")


def test_changed_background_prompt_invalidates_background_thumbnail_and_video(env):
    env.run("100", dry_run=True)
    env.prompt = env.prompt + " Add a lone cat."
    env.run("200", dry_run=True)
    assert env.image_calls == 1 and env.tts_calls == []
    assert env.manifest()["video_complete"]


def test_changed_renderer_invalidates_video_only(env):
    env.run("100", dry_run=True)
    env.mp.setattr(rr, "render_identity", lambda: "render-2")
    env.run("200", dry_run=True)
    assert env.tts_calls == [] and env.image_calls == 0 and env.manifest()["video_complete"]


def test_nothing_is_uploaded_until_everything_passed_and_status_calls_happen_once(env):
    env.run("100", fail_after_chunks=2)
    env.marked_exhausted = False
    env.run("200", fail_after_chunks=3)
    assert env.publish_calls == 0
    env.marked_exhausted = False
    env.run("300")
    assert env.publish_calls == 1 and env.notify_calls == 1
    final = env.run("400")  # already delivered: nothing happens again
    assert final[-1]["status"] == "COMPLETE" and env.publish_calls == 0 and env.notify_calls == 0


def test_upload_failure_after_video_checkpoint_does_not_rerender(env):
    env.run("100", dry_run=True)
    env.mp.setattr(rr, "publish_to_archive", lambda story: (_ for _ in ()).throw(RuntimeError("archive down")))
    with pytest.raises(RuntimeError):
        env.run("200")
    env.mp.setattr(rr, "publish_to_archive", lambda story: {"identifier": IDENTIFIER, "video_size": 1, "video_sha256": "x"})
    results = env.run("300")
    assert results[-1]["status"] == "COMPLETE" and env.tts_calls == [] and env.image_calls == 0


def test_manifest_and_snapshots_contain_no_secrets(env):
    env.run("100", dry_run=True)
    blob = ""
    for path in (env.tmp / "artifacts").rglob("manifest.json"):
        blob += path.read_text(encoding="utf-8")
    assert blob and "SECRET-GEMINI-KEY" not in blob and "SECRET-IA-KEY" not in blob
    assert "ghp_" not in blob and "token" not in blob.lower()


def test_incompatible_snapshot_is_never_restored(env):
    env.run("100", dry_run=True)
    manifest = json.loads(next((env.tmp / "artifacts").glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert cp.compatible(manifest, SLUG, IDENTIFIER, manifest["repair_hash"])
    assert not cp.compatible(manifest, "other-episode", IDENTIFIER, manifest["repair_hash"])
    assert not cp.compatible(manifest, SLUG, "other-identifier", manifest["repair_hash"])
    assert not cp.compatible(manifest, SLUG, IDENTIFIER, "0" * 64)


def test_quota_already_exhausted_makes_no_provider_call(env):
    env.marked_exhausted = True
    results = env.run("100")
    assert results[-1]["status"] == rr.STATUS_WAITING
    assert env.tts_calls == []
    assert cp.completed_chunks(env.manifest()) == []


def test_waiting_exit_is_not_an_exception_and_main_returns(env, capsys):
    env.marked_exhausted = True
    env.new_runner("100")
    env.mp.setattr(sys, "argv", ["repair_review.py", "--identifier", IDENTIFIER])
    env.mp.delenv("GITHUB_REPOSITORY", raising=False)
    for _ in range(3):  # background, thumbnail, then the first chunk finds the quota exhausted
        rr.main()
    assert "REPAIR_WAITING_FOR_TTS_QUOTA" in capsys.readouterr().out


# --- workflow and storage rules ---------------------------------------------------------------------------------

WORKFLOW = (ROOT / ".github" / "workflows" / "repair-review.yml").read_text(encoding="utf-8")
ACTION = (ROOT / ".github" / "actions" / "repair-unit" / "action.yml").read_text(encoding="utf-8")


def test_workflow_concurrency_is_scoped_to_the_archive_identifier():
    block = WORKFLOW.split("concurrency:")[1].split("jobs:")[0]
    assert "group: repair-review-${{ inputs.identifier }}" in block
    assert "production" not in block
    assert "cancel-in-progress: false" in block


def test_checkpoint_upload_runs_even_after_failure_with_30_day_retention():
    upload = ACTION.split("Upload checkpoint snapshot")[1]
    assert "if: always() && steps.unit.outputs.snapshot != ''" in upload
    assert "uses: actions/upload-artifact@v4" in upload
    assert "retention-days: 30" in upload
    assert "path: repair-snapshot" in upload


def test_each_unit_step_continues_only_while_work_remains_and_state_persists_always():
    ids = re.findall(r"id: (u\d+)\n", WORKFLOW)
    assert len(ids) >= 20
    for previous, current in zip(ids, ids[1:]):
        step = WORKFLOW.split(f"id: {current}\n")[1].split("uses:")[0]
        assert f"if: steps.{previous}.outputs.more == 'true'" in step
    assert re.search(r"if: always\(\) && inputs.dry_run != 'true'\n\s+run: python scripts/git_sync.py", WORKFLOW)


def test_no_binary_media_goes_to_git_or_automation_state():
    source = (ROOT / "scripts" / "repair_checkpoint.py").read_text(encoding="utf-8")
    assert "_update_remote(lambda read: {name: body}" in source  # index is JSON text only
    workflow = (ROOT / ".github" / "workflows" / "repair-review.yml").read_text(encoding="utf-8")
    assert "git add" not in workflow and "git push" not in workflow


def test_repair_never_uses_a_paid_or_cloud_voice_and_never_publishes_to_youtube():
    for name in ("repair_review.py", "repair_checkpoint.py"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for forbidden in ("tts_cloud_engines", "GOOGLE_CLOUD", "youtube_publish", "edge_tts"):
            assert forbidden not in source


def test_repair_keeps_the_permanent_fixes():
    settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    assert settings["background_audio_volume"] == 0.03
    import image_generate
    import inspect
    assert "rounded_rectangle" not in inspect.getsource(image_generate.render_thumbnail)
