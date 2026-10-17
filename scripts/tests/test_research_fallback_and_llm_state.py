"""Research fallback keeps working, and LLM quota state survives runners via automation-state."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

import generate_daily
import llm_router
import state_store
from llm_router import LlmQuotaWaiting, QuotaState

TOPIC = {
    "topic_ar": "واحة الموتس", "angle_ar": "الأفلاج", "period_ar": "القرن الثامن عشر",
    "search_query_en": "falaj oasis history",
}


def results(*domains: str) -> list[dict]:
    return [{"title": f"S{i}", "url": f"https://{domain}/p{i}", "excerpt": "evidence"} for i, domain in enumerate(domains)]


def quota_error():
    return urllib.error.HTTPError("https://api.exa.ai/search", 429, "Too Many", {}, io.BytesIO(b"{}"))


def test_gemini_grounding_failure_falls_back_to_exa_without_model_memory():
    notes = "ملاحظات https://a.org/1 https://b.org/2 https://c.org/3\n## خريطة الادعاءات"

    def fake_gemini(prompt, **kwargs):
        if kwargs.get("search"):
            raise RuntimeError("GROUNDED_RESEARCH_UNAVAILABLE: WAITING_FOR_LLM_QUOTA")
        return notes, []

    exa = mock.Mock(return_value=results("a.org", "b.org", "c.org"))
    providers = (("exa", "EXA_API_KEY", exa), ("brave", "BRAVE_SEARCH_API_KEY", mock.Mock()), ("firecrawl", "FIRECRAWL_API_KEY", mock.Mock()))
    with mock.patch.object(generate_daily, "production_rules", return_value=""), \
            mock.patch.object(generate_daily, "gemini", side_effect=fake_gemini), \
            mock.patch.object(generate_daily, "SEARCH_PROVIDERS", providers), \
            mock.patch.object(generate_daily, "load_secrets", return_value={"EXA_API_KEY": "k"}):
        research, sources = generate_daily.research_topic(TOPIC)
    exa.assert_called()
    assert {s["provider"] for s in sources} == {"exa"} and len(sources) == 3
    assert "https://a.org/p0" in research        # direct source links are always attached
    import run_summary
    assert run_summary.load()["search_provider_used"] == "exa"


def test_exa_failure_moves_to_the_next_configured_provider():
    exa = mock.Mock(side_effect=quota_error())
    brave = mock.Mock(return_value=results("a.org", "b.org", "c.org"))
    providers = (("exa", "EXA_API_KEY", exa), ("brave", "BRAVE_SEARCH_API_KEY", brave), ("firecrawl", "FIRECRAWL_API_KEY", mock.Mock()))
    secrets = {"EXA_API_KEY": "a", "BRAVE_SEARCH_API_KEY": "b", "FIRECRAWL_API_KEY": "c"}
    with mock.patch.object(generate_daily, "SEARCH_PROVIDERS", providers), mock.patch.object(generate_daily, "load_secrets", return_value=secrets):
        provider, found = generate_daily._collect_sources(TOPIC)
    assert provider == "brave" and len(found) == 3


def test_collected_sources_are_reused_when_the_digest_step_hits_a_quota_wait(tmp_path):
    cache = tmp_path / "logs" / "research_search_results.json"
    exa = mock.Mock(return_value=results("a.org", "b.org", "c.org"))
    providers = (("exa", "EXA_API_KEY", exa),)
    waiting = LlmQuotaWaiting("WAITING_FOR_LLM_QUOTA", kind="per_minute_model", scope="model",
                              blocked_until="2026-10-18T07:00:00+00:00", models_attempted=["m"])
    with mock.patch.object(generate_daily, "SEARCH_PROVIDERS", providers), \
            mock.patch.object(generate_daily, "load_secrets", return_value={"EXA_API_KEY": "k"}), \
            mock.patch.object(generate_daily, "gemini", side_effect=waiting):
        with pytest.raises(LlmQuotaWaiting):
            generate_daily.research_from_search_providers(TOPIC, "prompt", cache)
    assert exa.call_count == 2 and len(json.loads(cache.read_text(encoding="utf-8"))["results"]) == 3  # 2 queries, once
    exa.reset_mock()
    with mock.patch.object(generate_daily, "SEARCH_PROVIDERS", providers), \
            mock.patch.object(generate_daily, "load_secrets", return_value={"EXA_API_KEY": "k"}), \
            mock.patch.object(generate_daily, "gemini", return_value=("ملاحظات [1]", [])):
        research, sources = generate_daily.research_from_search_providers(TOPIC, "prompt", cache)
    exa.assert_not_called()                       # the sources collected earlier are not thrown away
    assert len(sources) == 3 and "https://a.org/p0" in research


# ---- llm_quota_state.json rides on automation-state --------------------------------------------

def git(cwd, *args):
    result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.x", *args],
                            cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "config").mkdir()
    (seed / "config" / "topics_history.json").write_text("[]\n", encoding="utf-8")
    (seed / "config" / "llm_quota_state.json").write_text("{}\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(bare), str(tmp_path / "runner1"))
    git(tmp_path, "clone", "-q", str(bare), str(tmp_path / "runner2"))
    monkeypatch.setattr(state_store, "REMOTE", "origin")
    return tmp_path, bare


def test_llm_quota_state_is_a_runtime_file_hydrated_and_persisted_through_automation_state(remote, monkeypatch):
    tmp, bare = remote
    assert "llm_quota_state.json" in state_store.RUNTIME_FILES
    main_before = git(bare, "rev-parse", "main")
    # Runner 1 exhausts the daily quota and persists.
    monkeypatch.setattr(state_store, "ROOT", tmp / "runner1")
    state_path = tmp / "runner1" / "config" / "llm_quota_state.json"
    router_state = QuotaState(state_path)
    info = llm_router.classify_gemini_error(429, json.dumps({"error": {"status": "RESOURCE_EXHAUSTED", "message": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}))
    now = llm_router.utcnow()
    router_state.block("model", "gemini-3.5-flash-lite", info, llm_router.next_daily_reset(now), now)
    assert state_store.persist_all()
    assert git(bare, "rev-parse", "main") == main_before                 # runtime state never goes to main
    assert "llm_quota_state.json" in git(bare, "ls-tree", "--name-only", "automation-state")
    # Runner 2 starts fresh, hydrates, and knows the model is exhausted without calling the API.
    monkeypatch.setattr(state_store, "ROOT", tmp / "runner2")
    assert "llm_quota_state.json" in state_store.hydrate_runtime_files()
    fresh = QuotaState(tmp / "runner2" / "config" / "llm_quota_state.json")
    assert fresh.blocked_until("gemini-3.5-flash-lite", now) is not None
    calls = []
    assert llm_router.route(["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
                            lambda model: calls.append(model) or model, state=fresh) == "gemini-3.1-flash-lite"
    assert calls == ["gemini-3.1-flash-lite"]


def test_tts_state_files_are_hydrated_and_persisted_too():
    assert {"tts_quota_state.json", "tts_usage.json", "llm_quota_state.json"} <= set(state_store.RUNTIME_FILES)
