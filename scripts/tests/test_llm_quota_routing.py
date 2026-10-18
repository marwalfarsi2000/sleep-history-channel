"""Quota-aware Gemini writing router (no network; every HTTP call is mocked)."""

from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error
from unittest import mock

import pytest

import generate_daily
import llm_router
from llm_router import GeminiApiError, LlmQuotaWaiting, QuotaKind, QuotaState

MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview"]
START = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.timezone.utc)


def quota_body(quota_id: str, retry_delay: str | None = None, status: str = "RESOURCE_EXHAUSTED") -> str:
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": "generativelanguage.googleapis.com/x", "quotaId": quota_id}]}]
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return json.dumps({"error": {"code": 429, "status": status, "message": "You exceeded your current quota", "details": details}})


PER_MINUTE_MODEL = quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")   # Run #49
PER_MINUTE_PROJECT = quota_body("GenerateRequestsPerMinutePerProject-FreeTier")
DAILY_MODEL = quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier")
TOKENS = quota_body("GenerateContentInputTokensPerModelPerMinute-FreeTier")


def api_error(status: int, body: str, headers: dict | None = None) -> GeminiApiError:
    info = llm_router.classify_gemini_error(status, body, headers)
    return GeminiApiError(f"Gemini HTTP {status}: {body[:100]}", info)


class Clock:
    def __init__(self):
        self.now = START
        self.slept: list[float] = []

    def utcnow(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(llm_router, "utcnow", fake.utcnow)
    monkeypatch.setattr(llm_router.time, "sleep", fake.sleep)
    return fake


@pytest.mark.parametrize("status,body,headers,kind,scope", [
    (429, PER_MINUTE_MODEL, None, QuotaKind.PER_MINUTE_MODEL, "model"),
    (429, PER_MINUTE_PROJECT, None, QuotaKind.PER_MINUTE_PROJECT, "project"),
    (429, DAILY_MODEL, None, QuotaKind.DAILY, "model"),
    (429, quota_body("GenerateRequestsPerDayPerProject-FreeTier"), None, QuotaKind.DAILY, "project"),
    (429, TOKENS, None, QuotaKind.TOKENS_PER_MINUTE, "model"),
    (503, '{"error":{"status":"UNAVAILABLE"}}', None, QuotaKind.CAPACITY, "model"),
    (429, '{"error":{"status":"RESOURCE_EXHAUSTED","message":"quota"}}', None, QuotaKind.UNKNOWN_EXHAUSTED, "model"),
    (401, '{"error":{"status":"UNAUTHENTICATED","message":"API key not valid"}}', None, QuotaKind.AUTH_CONFIG, None),
    (404, '{"error":{"status":"NOT_FOUND","message":"model not found"}}', None, QuotaKind.MODEL_NOT_FOUND, "model"),
    (400, '{"error":{"status":"INVALID_ARGUMENT","message":"Invalid request"}}', None, QuotaKind.OTHER, None),
])
def test_classifier_distinguishes_every_error_kind(status, body, headers, kind, scope):
    info = llm_router.classify_gemini_error(status, body, headers)
    assert (info.kind, info.scope) == (kind, scope)


def test_run_49_per_minute_error_is_a_rate_limit_not_a_final_failure():
    info = llm_router.classify_gemini_error(429, PER_MINUTE_MODEL)
    assert info.kind == QuotaKind.PER_MINUTE_MODEL and not info.daily
    assert info.quota_ids == ["GenerateRequestsPerMinutePerProjectPerModel-FreeTier"]


def test_primary_success_touches_no_state(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    assert llm_router.route(MODELS, lambda model: model, state=state) == MODELS[0]
    assert state.load() == {}


def test_per_minute_429_moves_to_the_next_model_and_primary_cools_down(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    calls = []

    def call(model):
        calls.append(model)
        if model == MODELS[0]:
            raise api_error(429, PER_MINUTE_MODEL)
        return model

    assert llm_router.route(MODELS, call, state=state) == MODELS[1]
    assert calls == [MODELS[0], MODELS[1]]
    entry = state.load()["models"][MODELS[0]]
    assert entry["status"] == "blocked" and entry["quota_kind"] == QuotaKind.PER_MINUTE_MODEL
    assert dt.datetime.fromisoformat(entry["blocked_until"]) > clock.now
    # While the cooldown is active the primary is skipped, not hammered.
    calls.clear()
    assert llm_router.route(MODELS, call, state=state) == MODELS[1]
    assert calls == [MODELS[1]]
    assert clock.slept == []


def test_expired_cooldown_clears_automatically_and_primary_is_used_again(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    outcomes = {MODELS[0]: [api_error(429, PER_MINUTE_MODEL), "ok"]}

    def call(model):
        queue = outcomes.get(model)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
        return model

    assert llm_router.route(MODELS, call, state=state) == MODELS[1]
    clock.now += dt.timedelta(seconds=120)
    assert llm_router.route(MODELS, call, state=state) == MODELS[0]
    assert "models" not in state.load()


def test_retry_after_header_and_retry_info_are_respected(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")

    def call(model):
        if model == MODELS[0]:
            raise api_error(429, PER_MINUTE_MODEL, {"Retry-After": "90"})
        if model == MODELS[1]:
            raise api_error(429, quota_body("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "34s"))
        return model

    assert llm_router.route(MODELS, call, state=state) == MODELS[2]
    blocked = state.load()["models"]
    assert dt.datetime.fromisoformat(blocked[MODELS[0]]["blocked_until"]) >= START + dt.timedelta(seconds=90)
    delay = dt.datetime.fromisoformat(blocked[MODELS[1]]["blocked_until"]) - START
    assert dt.timedelta(seconds=34) <= delay < dt.timedelta(seconds=60)


def test_all_models_minute_limited_waits_out_the_window_instead_of_failing(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    failing = set(MODELS)

    def call(model):
        if model in failing:
            failing.discard(model)          # each model recovers after its first 429
            raise api_error(429, PER_MINUTE_MODEL)
        return model

    assert llm_router.route(MODELS, call, state=state) == MODELS[0]
    assert len(clock.slept) == 1 and 0 < clock.slept[0] <= 70   # one bounded wait, no hammering


def test_shared_project_limit_stops_the_rotation_and_waits(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    calls = []

    def call(model):
        calls.append(model)
        if len(calls) == 1:
            raise api_error(429, PER_MINUTE_PROJECT)
        return model

    assert llm_router.route(MODELS, call, state=state) == MODELS[0]
    # One shared-quota error must not make us walk through all five models.
    assert calls == [MODELS[0], MODELS[0]] and len(clock.slept) == 1


def test_long_minute_block_becomes_a_planned_wait_not_a_failure(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    with pytest.raises(LlmQuotaWaiting) as raised:
        llm_router.route(MODELS, lambda model: (_ for _ in ()).throw(api_error(429, PER_MINUTE_MODEL, {"Retry-After": "900"})), state=state)
    waiting = raised.value
    assert str(waiting).startswith("WAITING_FOR_LLM_QUOTA")
    assert not waiting.daily and waiting.models_attempted == MODELS


def test_daily_quota_waits_for_the_quota_day_reset_without_sleeping(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    calls = []

    def call(model):
        calls.append(model)
        raise api_error(429, DAILY_MODEL)

    with pytest.raises(LlmQuotaWaiting) as raised:
        llm_router.route(MODELS, call, state=state)
    assert raised.value.daily and "daily quota" in str(raised.value)
    assert calls == MODELS and clock.slept == []
    entry = state.load()["models"][MODELS[0]]
    assert entry["quota_day"] == llm_router.quota_day(START) and entry["quota_kind"] == QuotaKind.DAILY
    assert dt.datetime.fromisoformat(entry["blocked_until"]) == llm_router.next_daily_reset(START)
    # The next run on the same quota day makes no API call at all.
    calls.clear()
    with pytest.raises(LlmQuotaWaiting):
        llm_router.route(MODELS, call, state=state)
    assert calls == []
    # After the reset the models are tried again.
    clock.now = llm_router.next_daily_reset(START) + dt.timedelta(minutes=1)
    assert llm_router.route(MODELS, lambda model: model, state=state) == MODELS[0]


def test_minute_quota_is_never_recorded_as_daily(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    with pytest.raises(LlmQuotaWaiting) as raised:
        llm_router.route(MODELS, lambda model: (_ for _ in ()).throw(api_error(429, PER_MINUTE_MODEL, {"Retry-After": "900"})), state=state)
    assert not raised.value.daily
    assert all("quota_day" not in entry for entry in state.load()["models"].values())


def test_auth_error_is_a_config_error_and_does_not_rotate_models(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    calls = []

    def call(model):
        calls.append(model)
        raise api_error(401, '{"error":{"status":"UNAUTHENTICATED","message":"API key not valid"}}')

    with pytest.raises(llm_router.LlmConfigError):
        llm_router.route(MODELS, call, state=state)
    assert calls == [MODELS[0]]


def test_retired_model_is_skipped_recorded_and_not_deleted_from_settings(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")

    def call(model):
        if model == MODELS[0]:
            raise api_error(404, '{"error":{"status":"NOT_FOUND"}}')
        return model

    assert llm_router.route(MODELS, call, state=state) == MODELS[1]
    assert state.load()["models"][MODELS[0]]["quota_kind"] == QuotaKind.MODEL_NOT_FOUND
    settings = json.loads((generate_daily.ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    assert "gemini-3.5-flash-lite" in [settings["llm_model"], *settings["llm_fallback_models"]]


def test_state_file_contains_no_secrets_or_request_bodies(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    llm_router.route(MODELS, lambda model: (_ for _ in ()).throw(api_error(429, PER_MINUTE_MODEL)) if model == MODELS[0] else model, state=state)
    text = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "key" not in text.lower().replace("quota_kind", "") and "prompt" not in text.lower()
    entry = state.load()["models"][MODELS[0]]
    assert set(entry) == {"status", "quota_kind", "blocked_until", "last_error_category", "updated_at"}


def test_search_grounding_never_waits_and_hands_over_to_search_providers(clock, tmp_path):
    state = QuotaState(tmp_path / "state.json")
    with pytest.raises(LlmQuotaWaiting):
        llm_router.route(MODELS, lambda model: (_ for _ in ()).throw(api_error(429, PER_MINUTE_MODEL)), state=state, allow_wait=False)
    assert clock.slept == []


# ---- through generate_daily.gemini with mocked HTTP --------------------------------------------

def http_error(code: int, body: str, headers: dict | None = None):
    return urllib.error.HTTPError("https://generativelanguage.googleapis.com", code, "e", headers or {}, io.BytesIO(body.encode("utf-8")))


class Ok:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps({"candidates": [{"content": {"parts": [{"text": "نص"}]}}]}).encode("utf-8")


SETTINGS = {
    "llm_model": MODELS[0], "llm_fallback_models": MODELS[1:], "research_fallback_models": [],
    "cost_policy": "free_or_preapproved_only", "approved_llm_models": MODELS,
}


def patched_gemini(responses):
    return (
        mock.patch.object(generate_daily, "SETTINGS", SETTINGS),
        mock.patch.object(generate_daily, "MODEL", MODELS[0]),
        mock.patch.object(generate_daily, "load_secrets", return_value={"GEMINI_API_KEY": "k"}),
        mock.patch.object(generate_daily.urllib.request, "urlopen", side_effect=responses),
    )


def test_gemini_call_falls_back_after_a_per_minute_429_without_retrying_in_place(clock):
    patches = patched_gemini([http_error(429, PER_MINUTE_MODEL), Ok()])
    with patches[0], patches[1], patches[2], patches[3] as urlopen:
        text, _ = generate_daily.gemini("prompt")
    assert text == "نص"
    urls = [call.args[0].full_url for call in urlopen.call_args_list]
    assert [MODELS[0] in urls[0], MODELS[1] in urls[1]] == [True, True] and len(urls) == 2


def test_all_models_exhausted_raises_planned_wait_not_a_validation_error(clock):
    patches = patched_gemini([http_error(429, DAILY_MODEL) for _ in MODELS])
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(LlmQuotaWaiting) as raised:
            generate_daily.gemini("prompt")
    assert "YOUTUBE_PACKAGING_VALIDATION_FAILED" not in str(raised.value)
    assert "WAITING_FOR_LLM_QUOTA" in str(raised.value)


def test_unapproved_or_paid_model_is_blocked_before_any_call():
    settings = dict(SETTINGS, llm_fallback_models=[*MODELS[1:], "gemini-2.5-pro"])
    with mock.patch.object(generate_daily, "SETTINGS", settings), mock.patch.object(generate_daily, "MODEL", MODELS[0]):
        with pytest.raises(RuntimeError, match="BLOCKED_COST"):
            generate_daily._llm_models()


def test_shipped_configuration_keeps_all_five_approved_writing_models_and_free_policy():
    settings = json.loads((generate_daily.ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    assert [settings["llm_model"], *settings["llm_fallback_models"]] == MODELS
    assert settings["cost_policy"] == "free_or_preapproved_only"
    assert set(MODELS) <= set(settings["approved_llm_models"])
