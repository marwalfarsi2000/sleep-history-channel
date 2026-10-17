"""Canonical per-run diagnostic state: ``.runtime/run_summary.json``.

Every process of a run (cloud_pipeline, generate_daily, pipeline, restore, notify)
reads and writes this one file. Observability must never break production, so every
public function swallows its own errors. No secrets, tokens or request bodies are
ever stored.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_PATH = ROOT / ".runtime" / "run_summary.json"
SERVICES = ("gemini", "tts", "image_generation", "internet_archive", "youtube", "telegram", "search")
MAX_ERROR_CHARS = 400

_REDACTIONS = (
    (re.compile(r"(?i)([?&](?:key|api_key|token|access_token)=)[^&\s'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|authorization|password)\b\s*[:=]\s*(?:bearer\s+)?\S+"), r"\1=[REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "[REDACTED]"),
    (re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b"), "[REDACTED]"),
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def redact(text: object) -> str:
    value = " ".join(str(text).split())
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value[:MAX_ERROR_CHARS]


def _blank(run_id: str) -> dict:
    return {
        "run_id": run_id, "episode_id": "", "story_path": "", "archive_identifier": "",
        "workflow": os.environ.get("GITHUB_WORKFLOW", ""), "action": "",
        "started_at": now_iso(), "finished_at": None, "status": "running",
        "current_stage": "", "failed_stage": None, "final_root_error": None,
        "error_category": None, "retryable": None, "cost_blocked": False,
        "owner_action_required": False, "owner_action": None,
        "waiting_for_quota": False, "quota_provider": None, "quota_model": None, "quota_kind": None,
        "blocked_until": None, "checkpoint_saved": False, "resume_stage": None,
        "restored_from_checkpoint": False, "reused_stages": [], "regenerated_stages": [],
        "models_attempted": [], "search_provider_used": None,
        "external_calls_summary": {service: 0 for service in SERVICES},
        "youtube_video_id": None,
    }


def _load() -> dict:
    try:
        value = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(summary: dict) -> None:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SUMMARY_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SUMMARY_PATH)


def _current_run_id() -> str:
    return os.environ.get("GITHUB_RUN_ID") or ""


def _mutate(change) -> None:
    try:
        summary = _load()
        if not summary:
            summary = _blank(_current_run_id() or uuid.uuid4().hex[:12])
        change(summary)
        _save(summary)
    except Exception:
        pass


def start(workflow: str = "", action: str = "", **fields: object) -> None:
    """Begin (or join) this run's summary; a stale file from another run is replaced."""
    def change(summary: dict) -> None:
        run_id = _current_run_id()
        if run_id and summary.get("run_id") != run_id:
            summary.clear()
            summary.update(_blank(run_id))
        if workflow:
            summary["workflow"] = workflow
        if action:
            summary["action"] = action
        summary.update(fields)
    _mutate(change)


def update(**fields: object) -> None:
    _mutate(lambda summary: summary.update(fields))


def stage(name: str) -> None:
    update(current_stage=name)


def count(service: str, amount: int = 1) -> None:
    def change(summary: dict) -> None:
        calls = summary.setdefault("external_calls_summary", {})
        calls[service] = int(calls.get(service, 0)) + amount
    _mutate(change)


def add_unique(field: str, value: str) -> None:
    def change(summary: dict) -> None:
        items = summary.setdefault(field, [])
        if value not in items:
            items.append(value)
    _mutate(change)


def reused(stage_name: str) -> None:
    add_unique("reused_stages", stage_name)


def regenerated(stage_name: str) -> None:
    add_unique("regenerated_stages", stage_name)


def set_search_provider(name: str) -> None:
    update(search_provider_used=name)


def classify(exc: BaseException) -> dict:
    """Map an exception to category/retryable/waiting/cost fields."""
    text = str(exc)
    upper = text.upper()
    result = {"error_category": None, "retryable": None, "waiting_for_quota": False, "cost_blocked": False}
    if type(exc).__name__ == "LlmQuotaWaiting" or "WAITING_FOR_LLM_QUOTA" in upper or "TTS_WAITING_FOR_QUOTA" in upper:
        result.update(error_category="QUOTA_WAIT", retryable=True, waiting_for_quota=True)
    elif "BLOCKED_COST" in upper:
        result.update(error_category="COST_BLOCKED", retryable=False, cost_blocked=True)
    else:
        try:
            from error_classifier import ErrorCategory, classify_error
            category = classify_error(exc) if isinstance(exc, Exception) else ErrorCategory.PERMANENT
        except Exception:
            category = "PERMANENT"
        result.update(error_category=category, retryable=category in ("TRANSIENT_RETRYABLE", "VALIDATION"))
    return result


def fail(exc: BaseException, *, overwrite: bool = False) -> None:
    """Record the final root error; an earlier, more specific record is kept."""
    def change(summary: dict) -> None:
        if summary.get("status") in ("failed", "waiting_for_quota") and summary.get("final_root_error") and not overwrite:
            summary["finished_at"] = summary.get("finished_at") or now_iso()
            return
        info = classify(exc)
        summary.update(info)
        summary["status"] = "waiting_for_quota" if info["waiting_for_quota"] else "failed"
        summary["failed_stage"] = summary.get("current_stage") or None
        summary["final_root_error"] = redact(exc)
        summary["finished_at"] = now_iso()
        if info["waiting_for_quota"]:
            summary["resume_stage"] = summary.get("current_stage") or None
            summary["quota_provider"] = "gemini"
            for attribute, field in (("quota_model", "quota_model"), ("kind", "quota_kind"), ("blocked_until", "blocked_until")):
                value = getattr(exc, attribute, None)
                if value:
                    summary[field] = value
            attempted = getattr(exc, "models_attempted", None)
            if attempted:
                for model in attempted:
                    if model not in summary.setdefault("models_attempted", []):
                        summary["models_attempted"].append(model)
    _mutate(change)


def finish(status: str = "success") -> None:
    def change(summary: dict) -> None:
        if summary.get("status") in ("failed", "waiting_for_quota"):
            return
        summary["status"] = status
        summary["finished_at"] = now_iso()
    _mutate(change)


def load() -> dict:
    return _load()
