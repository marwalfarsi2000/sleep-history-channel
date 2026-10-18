"""Verify the existing TTS fallback routing through the real pipeline.synthesize flow.

Gemini TTS primary -> Gemini TTS fallback (same narrator) -> on Gemini exhaustion mark the
quota day and regenerate the WHOLE narration with Google Cloud Wavenet-B -> Standard-B, within
the free monthly caps. Nothing here touches the network or the real config/ files.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import pipeline
import state_store

PRIMARY = "gemini-3.8-flash-tts"
FALLBACK = "gemini-3.8-flash-lite-tts"
WAVENET = "google-cloud:ar-XA-Wavenet-B"
STANDARD = "google-cloud:ar-XA-Standard-B"
CHUNK_TEXT = "نص تجريبي قصير للفقرة. " * 4  # ~92 characters per chunk


class Env:
    """Fake TTS engines plus a tmp story; records every engine call."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        (tmp_path / "config").mkdir(exist_ok=True)
        self.story = tmp_path / "story"
        (self.story / "tts_chunks").mkdir(parents=True)
        (self.story / "logs").mkdir()
        for number in range(1, 4):
            (self.story / "tts_chunks" / f"{number:03}.txt").write_text(CHUNK_TEXT + "\n", encoding="utf-8")
        self.settings = copy.deepcopy(pipeline.SETTINGS)
        monkeypatch.setattr(pipeline, "SETTINGS", self.settings)
        monkeypatch.setattr(pipeline, "SECRETS", {
            "GEMINI_API_KEY": "k", "GEMINI_TTS_VOICE_ID": "v", "GOOGLE_CLOUD_TTS_API_KEY": "gk"})
        monkeypatch.setattr(pipeline, "TTS_USAGE_PATH", tmp_path / "config" / "tts_usage.json")
        monkeypatch.setattr(pipeline, "TTS_QUOTA_STATE_PATH", tmp_path / "config" / "tts_quota_state.json")
        self.gemini_calls: list[tuple[str, str]] = []
        self.cloud_voices: list[str] = []
        self.gemini_behaviour = lambda model, index: "ok"
        self.fish = mock.Mock(side_effect=AssertionError("Fish must never be activated automatically"))
        self.munsit = mock.Mock(side_effect=AssertionError("Munsit must never be activated automatically"))
        monkeypatch.setattr(pipeline, "fish_tts_one", self.fish)
        monkeypatch.setattr(pipeline, "munsit_tts_one", self.munsit)
        monkeypatch.setattr(pipeline, "gemini_tts_one", self.fake_gemini)
        monkeypatch.setattr(pipeline.urllib.request, "urlopen", self.fake_urlopen)
        monkeypatch.setattr(pipeline, "run", self.fake_run)
        monkeypatch.setattr(pipeline, "ffmpeg", lambda: "ffmpeg")
        monkeypatch.setattr(pipeline, "duration", lambda path: 3600.0)
        monkeypatch.setattr(pipeline, "prepare", lambda story: None)
        monkeypatch.setattr(pipeline, "validate_gemini_voice", lambda: None)
        monkeypatch.setattr(pipeline, "validate_audio", lambda story: {})
        monkeypatch.setattr(pipeline, "mix_background_audio", lambda narration, out: out.write_bytes(b"M" * 2000))

    def fake_gemini(self, text, out, model):
        index = len(self.gemini_calls)
        self.gemini_calls.append((model, text))
        if self.gemini_behaviour(model, index) == "exhausted":
            raise pipeline.TtsEngineExhausted(model, "test quota")
        out.write_bytes(b"G" * 2000)
        return model

    def fake_urlopen(self, request, timeout=0):
        body = json.loads(request.data.decode("utf-8"))
        self.cloud_voices.append(body["voice"]["name"])
        payload = json.dumps({"audioContent": base64.b64encode(b"C" * 2000).decode("ascii")}).encode("utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    def fake_run(self, cmd, check=True):
        target = Path(cmd[-1])
        if target.suffix in {".mp3", ".opus"}:
            from_cloud = any(str(part).endswith(".parts.txt") for part in cmd)
            target.write_bytes((b"C" if from_cloud else b"N") * 2000)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def synthesize(self):
        asyncio.run(pipeline.synthesize(self.story))

    def set_usage(self, wavenet=0, standard=0):
        usage = {pipeline._usage_month(): {"wavenet": wavenet, "standard": standard}}
        pipeline.TTS_USAGE_PATH.write_text(json.dumps(usage), encoding="utf-8")

    def usage(self):
        return json.loads(pipeline.TTS_USAGE_PATH.read_text(encoding="utf-8")).get(pipeline._usage_month(), {})

    def chunk_models(self):
        return json.loads((self.story / "logs" / "tts_chunk_models.json").read_text(encoding="utf-8"))

    def chunk_bytes(self):
        return {p.name: p.read_bytes()[:1] for p in sorted((self.story / "audio_chunks").glob("*.mp3"))}

    def exhaust_all_gemini(self, from_index=1):
        self.gemini_behaviour = lambda model, index: "exhausted" if index >= from_index else "ok"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def test_primary_gemini_tts_model_is_used_first(env):
    env.synthesize()
    assert [model for model, _ in env.gemini_calls] == [PRIMARY] * 3
    assert env.cloud_voices == []
    assert set(env.chunk_models().values()) == {PRIMARY}


def test_gemini_fallback_model_continues_with_the_same_narrator(env):
    env.gemini_behaviour = lambda model, index: "exhausted" if (model == PRIMARY and index >= 1) else "ok"
    env.synthesize()
    models = env.chunk_models()
    assert models["001.txt"] == PRIMARY
    assert models["002.txt"] == FALLBACK and models["003.txt"] == FALLBACK
    assert env.cloud_voices == []
    state = json.loads((env.story / "logs" / "tts_voice_state.json").read_text(encoding="utf-8"))
    assert state["voice"] == pipeline.GEMINI_VOICE
    assert not pipeline.gemini_quota_exhausted_today()
    assert set(env.chunk_bytes().values()) == {b"G"}


def test_all_gemini_exhausted_regenerates_whole_narration_with_wavenet(env):
    env.exhaust_all_gemini(from_index=1)
    env.synthesize()
    assert pipeline.gemini_quota_exhausted_today()
    state = json.loads(pipeline.TTS_QUOTA_STATE_PATH.read_text(encoding="utf-8"))
    assert state["gemini_exhausted_quota_day"] == pipeline.quota_day()
    assert set(env.chunk_models().values()) == {WAVENET}
    assert set(env.chunk_bytes().values()) == {b"C"}, "a Gemini-made chunk survived the narrator switch"
    assert set(env.cloud_voices) == {"ar-XA-Wavenet-B"} and len(env.cloud_voices) == 3
    assert len(env.gemini_calls) == 3, "Gemini must not be called again once exhausted"
    voice_state = json.loads((env.story / "logs" / "tts_voice_state.json").read_text(encoding="utf-8"))
    assert voice_state["voice"] == WAVENET
    assert env.usage()["wavenet"] == len(CHUNK_TEXT.strip()) * 3 and env.usage().get("standard", 0) == 0


def test_wavenet_monthly_cap_exhausted_falls_to_standard(env):
    cap = pipeline._google_cloud_config()["monthly_char_caps"]["wavenet"]
    env.set_usage(wavenet=cap)
    env.exhaust_all_gemini(from_index=0)
    env.synthesize()
    assert set(env.chunk_models().values()) == {STANDARD}
    assert set(env.cloud_voices) == {"ar-XA-Standard-B"}
    assert env.usage()["wavenet"] == cap
    assert set(env.chunk_bytes().values()) == {b"C"}


def test_wavenet_lacking_room_for_whole_episode_uses_standard_never_crossing_cap(env):
    caps = pipeline._google_cloud_config()["monthly_char_caps"]
    env.set_usage(wavenet=caps["wavenet"] - 50)
    env.exhaust_all_gemini(from_index=0)
    env.synthesize()
    assert set(env.chunk_models().values()) == {STANDARD}
    assert env.usage()["wavenet"] == caps["wavenet"] - 50
    assert env.usage()["wavenet"] <= caps["wavenet"] and env.usage()["standard"] <= caps["standard"]


def test_both_cloud_caps_exhausted_raises_and_never_calls_a_cloud_engine(env):
    caps = pipeline._google_cloud_config()["monthly_char_caps"]
    env.set_usage(wavenet=caps["wavenet"], standard=caps["standard"])
    before = pipeline.TTS_USAGE_PATH.read_text(encoding="utf-8")
    env.exhaust_all_gemini(from_index=0)
    with pytest.raises(RuntimeError, match="exhausted"):
        env.synthesize()
    assert env.cloud_voices == []
    assert pipeline.TTS_USAGE_PATH.read_text(encoding="utf-8") == before
    env.fish.assert_not_called()
    env.munsit.assert_not_called()


def test_one_narrator_per_episode_and_resume_does_not_return_to_gemini(env):
    env.exhaust_all_gemini(from_index=1)
    env.synthesize()
    gemini_calls_after_first_run = len(env.gemini_calls)
    (env.story / "logs" / "stage_manifest.json").unlink()
    for name in ("12_audio_narration.mp3", "12_audio_full.mp3", "13_audio_review.opus"):
        (env.story / name).unlink(missing_ok=True)
    env.gemini_behaviour = lambda model, index: "ok"
    env.synthesize()
    assert len(env.gemini_calls) == gemini_calls_after_first_run
    assert set(env.chunk_models().values()) == {WAVENET}
    assert set(env.chunk_bytes().values()) == {b"C"}


def test_new_episode_on_an_exhausted_quota_day_starts_with_cloud_not_gemini(env):
    pipeline.mark_gemini_quota_exhausted("test")
    env.gemini_behaviour = lambda model, index: "ok"
    env.synthesize()
    assert env.gemini_calls == []
    assert set(env.chunk_models().values()) == {WAVENET}


def test_quota_state_and_usage_are_runtime_files_that_survive_runners(tmp_path, monkeypatch):
    assert {"tts_quota_state.json", "tts_usage.json"} <= set(state_store.RUNTIME_FILES)

    def git(cwd, *args):
        result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                                cwd=cwd, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "config").mkdir()
    (seed / "config" / "topics_history.json").write_text("[]\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    monkeypatch.setattr(state_store, "REMOTE", "origin")

    first = tmp_path / "runner1"
    git(tmp_path, "clone", "-q", str(bare), str(first))
    monkeypatch.setattr(state_store, "ROOT", first)
    monkeypatch.setattr(pipeline, "TTS_QUOTA_STATE_PATH", first / "config" / "tts_quota_state.json")
    monkeypatch.setattr(pipeline, "TTS_USAGE_PATH", first / "config" / "tts_usage.json")
    pipeline.mark_gemini_quota_exhausted("daily limit")
    pipeline._record_google_cloud_usage("wavenet", 1234)
    assert state_store.persist_all() is True

    second = tmp_path / "runner2"
    git(tmp_path, "clone", "-q", str(bare), str(second))
    assert not (second / "config" / "tts_quota_state.json").exists()
    monkeypatch.setattr(state_store, "ROOT", second)
    monkeypatch.setattr(pipeline, "TTS_QUOTA_STATE_PATH", second / "config" / "tts_quota_state.json")
    monkeypatch.setattr(pipeline, "TTS_USAGE_PATH", second / "config" / "tts_usage.json")
    hydrated = state_store.hydrate_runtime_files()
    assert {"tts_quota_state.json", "tts_usage.json"} <= set(hydrated)
    assert pipeline.gemini_quota_exhausted_today()
    assert pipeline.google_cloud_usage("wavenet") == 1234


def test_fish_and_munsit_are_never_in_the_fallback_chain(env):
    assert env.settings["tts_provider"] == "gemini"
    assert pipeline.tts_cloud_engines() == [WAVENET, STANDARD]
    approved = " ".join(env.settings["approved_tts_models"]).lower()
    assert "fish" not in approved and "munsit" not in approved
    caps = pipeline._google_cloud_config()["monthly_char_caps"]
    env.set_usage(wavenet=caps["wavenet"], standard=caps["standard"])
    env.exhaust_all_gemini(from_index=0)
    with pytest.raises(RuntimeError):
        env.synthesize()
    env.fish.assert_not_called()
    env.munsit.assert_not_called()


def test_only_free_approved_tts_models_are_configured_and_enforced(env):
    assert env.settings["cost_policy"] == "free_or_preapproved_only"
    assert env.settings["approved_tts_models"] == [PRIMARY, FALLBACK, WAVENET, STANDARD]
    assert pipeline._gemini_tts_models() == [PRIMARY, FALLBACK]
    for voice in env.settings["google_cloud_tts"]["voices"]:
        assert pipeline.GOOGLE_CLOUD_PREFIX + voice["name"] in env.settings["approved_tts_models"]
    env.settings["gemini_tts_fallback_models"] = ["gemini-3.8-pro-tts"]
    with pytest.raises(RuntimeError, match="BLOCKED_COST"):
        pipeline._gemini_tts_models()
    env.settings["gemini_tts_fallback_models"] = [FALLBACK]
    env.settings["cost_policy"] = "paid_ok"
    with pytest.raises(RuntimeError, match="BLOCKED_COST"):
        pipeline._gemini_tts_models()
