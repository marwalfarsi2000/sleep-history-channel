from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def failed_stage() -> str:
    outcomes = (
        ("تثبيت المتطلبات", os.environ.get("INSTALL_OUTCOME")),
        ("استعادة آخر نقطة محفوظة", os.environ.get("RESTORE_OUTCOME")),
        ("إنشاء القصة/الصوت/الفيديو أو الأرشفة", os.environ.get("PRODUCE_OUTCOME")),
    )
    return next((label for label, outcome in outcomes if outcome == "failure"), "مرحلة غير محددة")


def _json_error_message(raw: str) -> str:
    """Read the real API error message even when the JSON payload spans several lines."""
    starts = [match.start() for match in re.finditer(r'"error"\s*:\s*\{', raw)]
    if not starts:
        return ""
    message = re.search(r'"message"\s*:\s*"(?P<text>(?:[^"\\]|\\.)*)"', raw[starts[-1]:], re.S)
    return message.group("text") if message else ""


def diagnostic_summary() -> str:
    candidates = sorted((ROOT / "logs").glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        return "لا يوجد سجل تشخيصي محفوظ."
    raw = candidates[0].read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    useful = [
        line.strip()
        for line in lines
        if line.strip() and re.search(r"runtimeerror|httperror|gemini http|quota|unavailable|missing", line, re.I)
    ]
    summary = _json_error_message(raw)
    if not summary:
        if useful:
            json_lines = [line for line in useful if '{"error":' in line]
            summary = json_lines[-1] if json_lines else useful[-1]
        else:
            summary = lines[-1] if lines else "سجل فارغ"
    summary = re.sub(r"(?i)(api[_-]?key|token|secret|authorization)\s*[:=]\s*\S+", r"\1=[REDACTED]", summary)
    summary = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", summary)
    if len(summary) > 300:
        summary = summary[:300]
    return summary.strip()


STAGE_LABELS = {
    "produce": "بدء الإنتاج", "topic_selection": "اختيار الموضوع", "research": "البحث والمصادر",
    "research_alignment": "مواءمة البحث مع الزاوية",
    "outline": "مخطط الحلقة", "chapters": "كتابة الفصول", "quality_gates": "بوابات الجودة والتحقق من الحقائق",
    "packaging": "حزمة YouTube (العنوان والصورة المصغرة)", "prepare": "تجهيز نص الصوت",
    "synthesize": "توليد الصوت", "background": "الصورة الخلفية", "render": "تصيير الفيديو",
    "validate": "فحص الجودة النهائي", "package": "تجهيز حزمة المراجعة",
    "archive": "رفع نسخة المراجعة", "publish": "النشر",
}

COMPONENT_LABELS = {
    "topic_discovery": "Topic Discovery", "topic_gate": "Topic Gate", "candidate_bank": "Candidate Bank",
    "grounded_research": "Grounded Research", "source_validation": "Source Validation",
    "outline_generation": "Outline Generation", "chapter_writer": "Chapter Writer", "story_length": "Story Length",
    "story_quality_gate": "Story Quality Gate", "fact_check": "Fact Check", "youtube_packaging": "YouTube Packaging",
    "pronunciation": "Pronunciation", "tts_text": "TTS Text", "tts_gate": "TTS Gate", "tts_chunking": "TTS Chunking",
    "tts": "TTS", "audio_qc": "Audio QC", "llm_router": "LLM Router",
    "historical_accuracy_gate": "Historical Accuracy Gate", "thumbnail_text": "Thumbnail Text",
    "thumbnail_typography": "Thumbnail Typography", "image_provider": "Image Provider",
    "provider_quota": "Provider Quota", "cost_policy": "Cost Policy", "gemini_auth": "Gemini Auth",
    "credentials": "Credentials", "internet_archive": "Internet Archive", "youtube_auth": "YouTube Auth",
    "youtube_publish": "YouTube Publish", "approval_gate": "Approval Gate", "approval_state": "Approval State",
    "publish_preflight": "Publish Preflight", "research_alignment_gate": "Research ↔ Topic Alignment",
}


def load_run_summary() -> dict | None:
    """The structured summary of THIS run, or None if absent/stale (then logs are the fallback)."""
    import run_summary
    summary = run_summary.load()
    if summary.get("status") not in ("failed", "waiting_for_quota") or not summary.get("final_root_error"):
        return None
    current = os.environ.get("GITHUB_RUN_ID", "")
    if current and summary.get("run_id") and summary["run_id"] != current:
        return None
    return summary


def _link(run_url: str) -> str:
    return f'<a href="{html.escape(run_url, quote=True)}">فتح سجل التشغيل</a>'


def message_for_summary(summary: dict, run_url: str) -> str:
    stage = STAGE_LABELS.get(str(summary.get("failed_stage") or summary.get("current_stage") or ""), "")
    resume = STAGE_LABELS.get(str(summary.get("resume_stage") or ""), stage)
    error = html.escape(str(summary.get("final_root_error") or ""))
    root_error = str(summary.get("final_root_error") or "")
    if summary.get("waiting_for_quota"):
        if "TTS_WAITING_FOR_QUOTA" in root_error:
            return (
                "<b>⏸️ الحلقة بانتظار حصة الراوي</b>\n\n"
                "انتهت حصة صوت Gemini لليوم. المقاطع المنجزة محفوظة، "
                "وستكمل الحلقة تلقائيًا في التشغيل القادم بنفس الراوي. لا يلزم أي إجراء.\n"
                + _link(run_url)
            )
        where = f"\n📍 المرحلة المحفوظة: <b>{html.escape(resume)}</b>" if resume else ""
        return (
            "<b>⏸️ توقفت الحلقة مؤقتًا بسبب حصة نماذج الكتابة.</b>\n\n"
            "✅ الموضوع والبحث والعمل المنجز محفوظ."
            f"{where}\n\n"
            "🔄 التشغيل القادم سيكمل من نفس النقطة ولن يبدأ من الصفر.\n\n"
            "لا يلزم أي إجراء منك.\n"
            + _link(run_url)
        )
    return _failure_message(summary, stage, resume, error, run_url)


def _code_location(summary: dict) -> str:
    repo_file, function = summary.get("repo_file"), summary.get("repo_function")
    if not repo_file:
        return ""
    name = str(repo_file).split("/")[-1]
    return f"{name} → {function}()" if function and function != "<module>" else name


def _failure_message(summary: dict, stage: str, resume: str, error: str, run_url: str) -> str:
    import failure_registry
    episode = html.escape(str(summary.get("story_path") or summary.get("episode_id") or "").split("/")[-1])
    code = str(summary.get("error_code") or "")
    known = failure_registry.REGISTRY.get(code)
    component = str(summary.get("failed_component") or "")
    lines = ["<b>توقفت الأتمتة ⚠️</b>", ""]
    if episode:
        lines.append(f"الحلقة: <code>{episode}</code>")
    lines.append(f"المرحلة: <b>{html.escape(stage or 'غير محددة')}</b>")
    if component:
        lines.append(f"المكوّن: <b>{html.escape(COMPONENT_LABELS.get(component, component.replace('_', ' ').title()))}</b>")
    if known and code:
        lines.append(f"السبب: <code>{html.escape(code)}</code>")
        if known.detail_ar:
            lines.append(f"التفصيل: {html.escape(known.detail_ar)}")
        lines.append(f"الخطأ الأصلي: <code>{error}</code>")
    else:
        lines.append(f"السبب: <code>{error}</code>")
    if summary.get("cost_blocked"):
        lines.append("🚫 محظور بسياسة التكلفة: لم يُستخدم أي مزود مدفوع.")
    if summary.get("checkpoint_saved"):
        done = STAGE_LABELS.get(str(summary.get("last_completed_stage") or ""), "")
        lines.append(f"✅ محفوظ: {html.escape(done)}" if done else "✅ العمل المنجز محفوظ")
        upcoming = STAGE_LABELS.get(str(summary.get("resume_stage") or ""), "")
        if upcoming:
            lines.append(f"▶️ سيكمل من: {html.escape(upcoming)}")
    attempts = len(summary.get("auto_recovery_log") or [])
    if summary.get("auto_recovery_attempted") and attempts:
        lines.append(f"🔁 حاول النظام الاستئناف تلقائيًا {attempts} مرة دون نجاح.")
    policy = summary.get("recovery_policy") if "recovery_policy" in summary else "LEGACY"
    if policy == failure_registry.AUTO_RECOVERABLE:
        lines.append("🔄 يستطيع النظام معالجة هذا تلقائيًا." + (
            f" سيُستأنف من آخر نقطة محفوظة{f' ({html.escape(resume)})' if resume else ''}."
            if summary.get("checkpoint_saved") else ""
        ))
    elif policy == failure_registry.OWNER_ACTION:
        action = (known.action_ar if known else "") or str(summary.get("owner_action") or summary.get("recommended_action") or "")
        lines.append(f"👤 يلزم منك: <code>{html.escape(action)}</code>")
        if not summary.get("retryable"):
            lines.append("⛔ لن يُحل بإعادة التشغيل وحدها.")
    elif policy == failure_registry.CODE_DEFECT:
        where = _code_location(summary)
        lines.append("⛔ لن يُحل بإعادة التشغيل وحدها.")
        lines.append("🛠️ يحتاج تعديلًا برمجيًا" + (f" في: <code>{html.escape(where)}</code>" if where else "") + ".")
        lines.append("أرسلي هذه الرسالة إلى Codex دون بدء العمل من الصفر.")
    elif policy is None:
        lines.append("ℹ️ لم يُصنَّف السبب تلقائيًا؛ افتح سجل التشغيل لمعرفة التفاصيل.")
    else:  # summary written before failure diagnostics existed
        if summary.get("retryable"):
            lines.append("🔄 يمكن إعادة المحاولة؛ التشغيل القادم سيستخدم ما حُفظ.")
        else:
            lines.append("⛔ لن يُحل بإعادة التشغيل وحدها.")
    if summary.get("owner_action_required") and policy != failure_registry.OWNER_ACTION:
        lines.append(f"👤 يلزم إجراء منك: <code>{html.escape(str(summary.get('owner_action') or ''))}</code>")
    lines.append(_link(run_url))
    return "\n".join(lines)


def build_message(run_url: str) -> str:
    """Structured run summary first; log parsing only for legacy runs without one."""
    summary = load_run_summary()
    if summary:
        return message_for_summary(summary, run_url)
    log_summary = diagnostic_summary()
    if "TTS_WAITING_FOR_QUOTA" in log_summary:
        # Planned pause, not a fault: the narrator's daily quota is used up.
        return (
            "<b>⏸️ الحلقة بانتظار حصة الراوي</b>\n\n"
            "انتهت حصة صوت Gemini لليوم. المقاطع المنجزة محفوظة، "
            "وستكمل الحلقة تلقائيًا في التشغيل القادم بنفس الراوي. لا يلزم أي إجراء.\n"
            + _link(run_url)
        )
    return (
        "<b>توقفت أتمتة الفيديو ⚠️</b>\n\n"
        f"المرحلة: <b>{html.escape(failed_stage())}</b>\n"
        f"السبب المختصر: <code>{html.escape(log_summary)}</code>\n"
        f"{_link(run_url)}\n\n"
        "تم حفظ نقطة الاستكمال المتاحة. أرسلي هذه الرسالة إلى Codex لإصلاح السبب دون بدء العمل من الصفر."
    )


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are unavailable for failure notification")
    text = build_message(os.environ.get("RUN_URL", ""))
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the failure notification")


if __name__ == "__main__":
    main()
