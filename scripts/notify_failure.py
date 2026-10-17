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
    "outline": "مخطط الحلقة", "chapters": "كتابة الفصول", "quality_gates": "بوابات الجودة والتحقق من الحقائق",
    "packaging": "حزمة YouTube (العنوان والصورة المصغرة)", "prepare": "تجهيز نص الصوت",
    "synthesize": "توليد الصوت", "background": "الصورة الخلفية", "render": "تصيير الفيديو",
    "validate": "فحص الجودة النهائي", "archive": "رفع نسخة المراجعة", "publish": "النشر",
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
    episode = html.escape(str(summary.get("story_path") or summary.get("episode_id") or "").split("/")[-1])
    lines = ["<b>توقفت أتمتة الفيديو ⚠️</b>", ""]
    if episode:
        lines.append(f"الحلقة: <code>{episode}</code>")
    lines.append(f"المرحلة: <b>{html.escape(stage or 'غير محددة')}</b>")
    lines.append(f"السبب الحقيقي: <code>{error}</code>")
    if summary.get("cost_blocked"):
        lines.append("🚫 محظور بسياسة التكلفة: لم يُستخدم أي مزود مدفوع.")
    if summary.get("retryable") and summary.get("checkpoint_saved"):
        lines.append(f"🔄 سيُستأنف تلقائيًا من آخر نقطة محفوظة{f' ({html.escape(resume)})' if resume else ''}.")
    elif summary.get("retryable"):
        lines.append("🔄 يمكن إعادة المحاولة؛ التشغيل القادم سيستخدم ما حُفظ.")
    else:
        lines.append("⛔ لن يُحل بإعادة التشغيل وحدها.")
    if summary.get("owner_action_required"):
        lines.append(f"👤 يلزم إجراء منك: <code>{html.escape(str(summary.get('owner_action') or ''))}</code>")
    elif not summary.get("retryable"):
        lines.append("👤 يلزم إصلاح: أرسلي هذه الرسالة إلى Codex دون بدء العمل من الصفر.")
    else:
        lines.append("لا يلزم إجراء منك.")
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
