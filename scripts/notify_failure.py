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


def diagnostic_summary() -> str:
    candidates = sorted((ROOT / "logs").glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        return "لا يوجد سجل تشخيصي محفوظ."
    content = candidates[0].read_text(encoding="utf-8", errors="replace")

    # Try parsing embedded Gemini error JSON messages first
    for match in re.finditer(r'\{\s*"error"\s*:\s*\{[\s\S]*?\}\s*\}', content):
        try:
            err_data = json.loads(match.group(0))
            msg = err_data.get("error", {}).get("message")
            if msg:
                summary = str(msg).strip()
                summary = re.sub(r"(?i)(api[_-]?key|token|secret|authorization)\s*[:=]\s*\S+", r"\1=[REDACTED]", summary)
                return summary[:500].strip()
        except Exception:
            pass

    lines = content.splitlines()
    useful = [
        line.strip()
        for line in lines
        if line.strip() and re.search(r"error|runtimeerror|httperror|failed|quota|unavailable|missing", line, re.I)
        and not line.strip().startswith('"error"')
        and line.strip() != "{"
    ]
    summary = (useful[-1] if useful else lines[-1] if lines else "سجل فارغ")[:500]
    summary = re.sub(r"(?i)(api[_-]?key|token|secret|authorization)\s*[:=]\s*\S+", r"\1=[REDACTED]", summary)
    return summary


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are unavailable for failure notification")
    run_url = os.environ.get("RUN_URL", "")
    summary = diagnostic_summary()
    if "TTS_WAITING_FOR_QUOTA" in summary:
        # Planned pause, not a fault: the narrator's daily quota is used up.
        text = (
            "<b>⏸️ الحلقة بانتظار حصة الراوي</b>\n\n"
            "انتهت حصة صوت Gemini لليوم. المقاطع المنجزة محفوظة، "
            "وستكمل الحلقة تلقائيًا في التشغيل القادم بنفس الراوي. لا يلزم أي إجراء.\n"
            f'<a href="{html.escape(run_url, quote=True)}">فتح سجل التشغيل</a>'
        )
    else:
        text = None
    text = text or (
        "<b>توقفت أتمتة الفيديو ⚠️</b>\n\n"
        f"المرحلة: <b>{html.escape(failed_stage())}</b>\n"
        f"السبب المختصر: <code>{html.escape(summary)}</code>\n"
        f'<a href="{html.escape(run_url, quote=True)}">فتح سجل التشغيل</a>\n\n'
        "تم حفظ نقطة الاستكمال المتاحة. أرسلي هذه الرسالة إلى Codex لإصلاح السبب دون بدء العمل من الصفر."
    )
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
