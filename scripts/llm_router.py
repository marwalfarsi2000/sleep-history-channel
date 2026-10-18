"""Quota-aware routing for the approved Gemini writing models.

All approved models share one Gemini project, so a fast loop over them is only a
real fallback when the limit is per model. This module classifies every Gemini
error, remembers cooldowns in ``config/llm_quota_state.json`` (a runtime file that
lives on the automation-state branch), honours Retry-After, and turns exhausted
quota into a planned ``WAITING_FOR_LLM_QUOTA`` pause instead of a failure.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "config" / "llm_quota_state.json"

PER_MINUTE_DEFAULT_SECONDS = 65
CAPACITY_COOLDOWN_SECONDS = 30
UNKNOWN_COOLDOWN_SECONDS = 65
RETIRED_COOLDOWN_SECONDS = 24 * 3600
MAX_INLINE_WAIT_SECONDS = 240
MAX_ROUTING_PASSES = 4


class QuotaKind:
    PER_MINUTE_MODEL = "per_minute_model"
    PER_MINUTE_PROJECT = "per_minute_project"
    TOKENS_PER_MINUTE = "tokens_per_minute"
    DAILY = "daily"
    CAPACITY = "capacity"
    UNKNOWN_EXHAUSTED = "unknown_exhausted"
    AUTH_CONFIG = "auth_config"
    MODEL_NOT_FOUND = "model_not_found"
    OTHER = "other"


RATE_LIMIT_KINDS = {
    QuotaKind.PER_MINUTE_MODEL, QuotaKind.PER_MINUTE_PROJECT, QuotaKind.TOKENS_PER_MINUTE,
    QuotaKind.DAILY, QuotaKind.CAPACITY, QuotaKind.UNKNOWN_EXHAUSTED,
}


@dataclass
class ApiErrorInfo:
    http_status: int
    kind: str
    scope: str | None = None            # "model" or "project"
    retry_after: float | None = None
    quota_ids: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def daily(self) -> bool:
        return self.kind == QuotaKind.DAILY


class GeminiApiError(RuntimeError):
    """One failed Gemini HTTP call with its classification attached."""

    def __init__(self, message: str, info: ApiErrorInfo):
        super().__init__(message)
        self.info = info


class LlmQuotaWaiting(RuntimeError):
    """Every approved writing model is blocked; the episode must pause, not fail."""

    def __init__(self, message: str, *, kind: str, scope: str | None, blocked_until: str,
                 models_attempted: list[str], quota_model: str = "", daily: bool = False):
        super().__init__(message)
        self.kind = kind
        self.scope = scope
        self.blocked_until = blocked_until
        self.models_attempted = list(models_attempted)
        self.quota_model = quota_model
        self.daily = daily


class LlmConfigError(RuntimeError):
    """Authentication/configuration problem: rotating models cannot help."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def quota_day(now: dt.datetime | None = None) -> str:
    """Gemini daily quotas reset at midnight Pacific time."""
    now = now or utcnow()
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return (now - dt.timedelta(hours=8)).date().isoformat()


def next_daily_reset(now: dt.datetime | None = None) -> dt.datetime:
    now = now or utcnow()
    try:
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
        local = now.astimezone(pacific)
        tomorrow = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time.min, tzinfo=pacific)
        return tomorrow.astimezone(dt.timezone.utc)
    except Exception:
        shifted = now - dt.timedelta(hours=8)
        return dt.datetime.combine(shifted.date() + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc) + dt.timedelta(hours=8)


def _parse_retry_delay(value: object) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s?\s*", str(value or ""))
    return float(match.group(1)) if match else None


def classify_gemini_error(status: int, body: str, headers: object = None) -> ApiErrorInfo:
    """Classify a Gemini HTTP failure without keeping request data."""
    try:
        error = json.loads(body).get("error", {}) if body else {}
    except (json.JSONDecodeError, AttributeError):
        error = {}
    api_status = str(error.get("status", "")).upper()
    message = str(error.get("message", "")) or " ".join(body[:200].split())
    quota_ids: list[str] = []
    retry_after: float | None = None
    for detail in error.get("details", []) or []:
        for violation in detail.get("violations", []) or []:
            quota_id = violation.get("quotaId") or violation.get("quotaMetric")
            if quota_id:
                quota_ids.append(str(quota_id))
        if str(detail.get("@type", "")).endswith("RetryInfo"):
            retry_after = _parse_retry_delay(detail.get("retryDelay"))
    header_value = None
    if headers is not None:
        try:
            header_value = headers.get("Retry-After")
        except AttributeError:
            header_value = None
    if header_value:
        try:
            retry_after = max(retry_after or 0.0, float(header_value))
        except ValueError:
            pass
    evidence = " ".join([*quota_ids, message]).lower().replace("_", "").replace("-", "").replace(" ", "")
    per_model = "permodel" in evidence
    summary = f"{api_status} {', '.join(quota_ids)} {message[:140]}".strip()

    if status in (401,) or (status == 403 and any(m in message.lower() for m in ("api key", "api_key", "permission denied on resource project", "billing"))):
        return ApiErrorInfo(status, QuotaKind.AUTH_CONFIG, None, retry_after, quota_ids, summary)
    if status in (403, 404) or api_status in ("NOT_FOUND", "PERMISSION_DENIED"):
        return ApiErrorInfo(status, QuotaKind.MODEL_NOT_FOUND, "model", retry_after, quota_ids, summary)
    if status == 429 or api_status == "RESOURCE_EXHAUSTED":
        if any(m in evidence for m in ("perday", "requestsperday", "dailyquota", "quotaexceeded")) and "perminute" not in evidence:
            return ApiErrorInfo(status, QuotaKind.DAILY, "model" if per_model else "project", retry_after, quota_ids, summary)
        if "token" in evidence and "perminute" in evidence:
            return ApiErrorInfo(status, QuotaKind.TOKENS_PER_MINUTE, "model" if per_model else "project", retry_after, quota_ids, summary)
        if "perminute" in evidence:
            kind = QuotaKind.PER_MINUTE_MODEL if per_model else QuotaKind.PER_MINUTE_PROJECT
            return ApiErrorInfo(status, kind, "model" if per_model else "project", retry_after, quota_ids, summary)
        return ApiErrorInfo(status, QuotaKind.UNKNOWN_EXHAUSTED, "model", retry_after, quota_ids, summary)
    if status in (500, 502, 503, 504) or api_status in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"):
        return ApiErrorInfo(status, QuotaKind.CAPACITY, "model", retry_after, quota_ids, summary)
    return ApiErrorInfo(status, QuotaKind.OTHER, None, retry_after, quota_ids, summary)


class QuotaState:
    """Cooldown/daily state per model and for the shared project (no secrets)."""

    def __init__(self, path: Path | None = None):
        self.path = path

    def _path(self) -> Path:
        return self.path or STATE_PATH

    def load(self) -> dict:
        try:
            value = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def save(self, state: dict) -> None:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            print(f"Warning: could not save LLM quota state: {exc}", file=sys.stderr)

    @staticmethod
    def _active(entry: dict, now: dt.datetime) -> bool:
        until = entry.get("blocked_until")
        if not until:
            return False
        try:
            return dt.datetime.fromisoformat(until) > now
        except ValueError:
            return False

    def clear_expired(self, now: dt.datetime | None = None) -> dict:
        now = now or utcnow()
        state = self.load()
        changed = False
        if state.get("project") and not self._active(state["project"], now):
            del state["project"]
            changed = True
        for model in list((state.get("models") or {})):
            if not self._active(state["models"][model], now):
                del state["models"][model]
                changed = True
        if state.get("models") == {}:
            state.pop("models")
        if changed:
            self.save(state)
        return state

    def block(self, scope: str, model: str, info: ApiErrorInfo, until: dt.datetime, now: dt.datetime) -> None:
        state = self.load()
        entry = {
            "status": "blocked",
            "quota_kind": info.kind,
            "blocked_until": until.isoformat(),
            "last_error_category": info.kind,
            "updated_at": now.isoformat(),
        }
        if info.daily:
            entry["quota_day"] = quota_day(now)
        if scope == "project":
            state["project"] = entry
        else:
            state.setdefault("models", {})[model] = entry
        state["version"] = 1
        state["updated_at"] = now.isoformat()
        self.save(state)

    def blocked_until(self, model: str, now: dt.datetime) -> dt.datetime | None:
        state = self.load()
        candidates = []
        for entry in (state.get("project"), (state.get("models") or {}).get(model)):
            if entry and self._active(entry, now):
                candidates.append(dt.datetime.fromisoformat(entry["blocked_until"]))
        return max(candidates) if candidates else None

    def entry_for(self, model: str, now: dt.datetime) -> dict:
        state = self.load()
        for entry in ((state.get("models") or {}).get(model), state.get("project")):
            if entry and self._active(entry, now):
                return entry
        return {}


def _cooldown_until(info: ApiErrorInfo, now: dt.datetime) -> dt.datetime:
    if info.kind == QuotaKind.DAILY:
        return next_daily_reset(now)
    if info.kind == QuotaKind.MODEL_NOT_FOUND:
        return now + dt.timedelta(seconds=RETIRED_COOLDOWN_SECONDS)
    if info.kind == QuotaKind.CAPACITY:
        return now + dt.timedelta(seconds=max(CAPACITY_COOLDOWN_SECONDS, info.retry_after or 0))
    if info.kind in (QuotaKind.PER_MINUTE_MODEL, QuotaKind.PER_MINUTE_PROJECT, QuotaKind.TOKENS_PER_MINUTE):
        return now + dt.timedelta(seconds=max(PER_MINUTE_DEFAULT_SECONDS if info.retry_after is None else 0, (info.retry_after or 0) + 1))
    return now + dt.timedelta(seconds=max(UNKNOWN_COOLDOWN_SECONDS, info.retry_after or 0))


def route(
    models: list[str],
    call: Callable[[str], object],
    *,
    state: QuotaState | None = None,
    allow_wait: bool = True,
    report: Callable[..., None] | None = None,
):
    """Run ``call(model)`` on the first available approved model.

    ``call`` must raise :class:`GeminiApiError` for API failures. Per-model limits
    move on to the next model at once; project-wide limits and fully blocked chains
    wait out short per-minute windows (bounded) or raise :class:`LlmQuotaWaiting`.
    """
    state = state or QuotaState()
    attempted: list[str] = []
    waited = 0.0
    last_info: ApiErrorInfo | None = None
    last_model = ""
    for _pass in range(MAX_ROUTING_PASSES):
        now = utcnow()
        state.clear_expired(now)
        available = [m for m in models if state.blocked_until(m, now) is None]
        if not available:
            until = min(state.blocked_until(m, now) for m in models)  # type: ignore[type-var]
            entry = state.entry_for(models[0], now)
            wait = (until - now).total_seconds()
            daily = entry.get("quota_kind") == QuotaKind.DAILY
            if not allow_wait or daily or waited + wait > MAX_INLINE_WAIT_SECONDS:
                raise _waiting(models, attempted, last_info, last_model, until, state, now)
            time.sleep(max(wait, 0) + 1)
            waited += wait
            continue
        for model in available:
            attempted.append(model) if model not in attempted else None
            try:
                return call(model)
            except GeminiApiError as error:
                info = error.info
                last_info, last_model = info, model
                now = utcnow()
                if info.kind == QuotaKind.AUTH_CONFIG:
                    raise LlmConfigError(f"GEMINI_AUTH_CONFIG_ERROR: {info.summary}") from error
                if info.kind not in RATE_LIMIT_KINDS and info.kind != QuotaKind.MODEL_NOT_FOUND:
                    raise
                scope = "project" if info.scope == "project" else "model"
                state.block(scope, model, info, _cooldown_until(info, now), now)
                if report:
                    report(model=model, kind=info.kind, scope=scope)
                print(
                    f"Gemini model {model} unavailable ({info.kind}, scope={scope}); "
                    f"{'trying next approved model' if scope == 'model' else 'shared project limit'}",
                    file=sys.stderr, flush=True,
                )
                if scope == "project":
                    break
    now = utcnow()
    until = min((state.blocked_until(m, now) for m in models if state.blocked_until(m, now)), default=now)
    raise _waiting(models, attempted, last_info, last_model, until, state, now)


def _waiting(models, attempted, info, model, until, state, now) -> LlmQuotaWaiting:
    entry = state.entry_for(model or models[0], now)
    kind = (info.kind if info else entry.get("quota_kind")) or QuotaKind.UNKNOWN_EXHAUSTED
    daily = kind == QuotaKind.DAILY or entry.get("quota_kind") == QuotaKind.DAILY
    scope = info.scope if info else None
    label = "daily quota" if daily else "per-minute/token quota"
    return LlmQuotaWaiting(
        f"WAITING_FOR_LLM_QUOTA: every approved Gemini writing model is blocked ({label}, kind={kind}, "
        f"scope={scope}); the episode is saved and will resume after {until.isoformat()}",
        kind=kind, scope=scope, blocked_until=until.isoformat(), models_attempted=attempted,
        quota_model=model, daily=daily,
    )
