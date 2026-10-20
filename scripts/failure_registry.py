"""Central registry of known failures: what failed, where, and what should happen next.

Pure diagnostics. Nothing here changes pipeline behaviour or edits code; it only
turns an exception into a structured, secret-free description that
``run_summary`` stores and ``notify_failure`` renders for the owner.
"""

from __future__ import annotations

import re
import traceback
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AUTO_RECOVERABLE = "AUTO_RECOVERABLE"
OWNER_ACTION = "OWNER_ACTION"
CODE_DEFECT = "CODE_DEFECT"

VALIDATION = "DATA_OR_MODEL_OUTPUT_VALIDATION"
QUOTA_WAIT = "QUOTA_WAIT"
COST_BLOCKED = "COST_BLOCKED"
CONFIGURATION = "CONFIGURATION"
TRANSIENT = "TRANSIENT_RETRYABLE"
INVARIANT = "INTERNAL_INVARIANT"
UNCLASSIFIED = "UNCLASSIFIED"


@dataclass(frozen=True)
class Failure:
    code: str
    stage: str | None  # None: the stage tracked at run time is the truth
    component: str | None
    category: str
    policy: str | None  # AUTO_RECOVERABLE / OWNER_ACTION / CODE_DEFECT, None = cannot tell
    retryable: bool
    recommended_action: str
    owner_action_required: bool = False
    planned_wait: bool = False
    waiting_for_quota: bool = False
    cost_blocked: bool = False
    detail_ar: str = ""
    action_ar: str = ""
    wrapper: bool = False  # a generic parent-process message that hides the real error

    @property
    def immediate_retry(self) -> bool:
        """Worth another bounded attempt in the same job (quota waits and free-limit blocks are not)."""
        return (
            self.policy == AUTO_RECOVERABLE
            and self.retryable
            and self.category == TRANSIENT
            and not self.planned_wait
            and not self.cost_blocked
        )

    @property
    def max_attempts(self) -> int:
        return 3 if self.category == TRANSIENT else 2


def _f(code, stage, component, category, policy, retryable, action, **extra) -> tuple[str, Failure]:
    return code, Failure(code, stage, component, category, policy, retryable, action, **extra)


REGISTRY: dict[str, Failure] = dict([
    _f("TOPIC_DISCOVERY_FAILED", "topic_selection", "topic_discovery", VALIDATION, AUTO_RECOVERABLE, True,
       "Use valid fresh candidates plus queued candidate-bank entries before enforcing the minimum pool.",
       detail_ar="لم يتوفر عدد كافٍ من المرشحين الصالحين للموضوع."),
    _f("TOPIC_GATE_REJECTED", "topic_selection", "topic_gate", VALIDATION, AUTO_RECOVERABLE, True,
       "The topic gate rejected the candidates; the next run picks a fresh topic.",
       detail_ar="رفضت بوابة الموضوع المرشحين المتاحين."),
    _f("TOPIC_CANDIDATE_BANK_INVALID", "topic_selection", "candidate_bank", INVARIANT, CODE_DEFECT, False,
       "Repair config/topic_candidate_bank.json so it is a JSON list.",
       detail_ar="ملف بنك المرشحين غير صالح."),
    _f("GROUNDED_RESEARCH_UNAVAILABLE", "research", "grounded_research", TRANSIENT, AUTO_RECOVERABLE, True,
       "Search providers were unavailable; the next run repeats research.",
       detail_ar="مزودو البحث الموثّق غير متاحين الآن."),
    _f("SOURCE_VALIDATION_FAILED", "research", "source_validation", VALIDATION, AUTO_RECOVERABLE, True,
       "Research lacked verifiable sources; the next run repeats research.",
       detail_ar="لم تستوفِ نتائج البحث شروط المصادر."),
    _f("OUTLINE_GENERATION_FAILED_AFTER_RETRIES", "outline", "outline_generation", VALIDATION, AUTO_RECOVERABLE, True,
       "The model returned malformed outlines after retries; saved research is reused on the next run.",
       detail_ar="أعاد النموذج مخططًا غير صالح بعد عدة محاولات."),
    _f("OUTLINE_GENERATION_FAILED", "outline", "outline_generation", VALIDATION, AUTO_RECOVERABLE, True,
       "The model returned a malformed outline; saved research is reused on the next run.",
       detail_ar="أعاد النموذج مخططًا غير صالح."),
    _f("CHAPTER_LENGTH_TARGET_FAILED", "chapters", "chapter_writer", VALIDATION, AUTO_RECOVERABLE, True,
       "A chapter missed its length target; finished chapters are kept and the next run continues.",
       detail_ar="لم يبلغ أحد الفصول الطول المطلوب."),
    _f("STORY_TOO_SHORT", "chapters", "story_length", VALIDATION, AUTO_RECOVERABLE, True,
       "The assembled story is under 5000 Arabic words; the next run resumes the chapters stage.",
       detail_ar="القصة المجمّعة أقصر من الحد الأدنى."),
    _f("STORY_QUALITY_GATE_REJECTED", "quality_gates", "story_quality_gate", VALIDATION, AUTO_RECOVERABLE, False,
       "Targeted in-stage quality repair was exhausted; do not blind-retry the unchanged story.",
       detail_ar="لم تجتز القصة بوابة الجودة بعد استنفاد الإصلاح الموجّه داخل المرحلة."),
    _f("FACT_CORRECTION_FAILED", "quality_gates", "fact_check", VALIDATION, AUTO_RECOVERABLE, True,
       "Fact correction shortened the story too much; inspect logs/ if it repeats.",
       detail_ar="فشل تصحيح الحقائق."),
    _f("FINAL_FACT_CHECK_FAILED", "quality_gates", "fact_check", VALIDATION, AUTO_RECOVERABLE, True,
       "The final fact check still found problems; inspect logs/ if it repeats.",
       detail_ar="لم يجتز الفحص النهائي للحقائق."),
    _f("YOUTUBE_PACKAGING_VALIDATION_FAILED", "packaging", "youtube_packaging", VALIDATION, AUTO_RECOVERABLE, True,
       "Packaging fields failed validation; the next run regenerates the failing field.",
       detail_ar="لم يجتز أحد حقول حزمة YouTube التحقق."),
    _f("PRONUNCIATION_VALIDATION_FAILED", "prepare", "pronunciation", VALIDATION, AUTO_RECOVERABLE, True,
       "The pronunciation pass changed the text; the next run repeats it.",
       detail_ar="غيّر فحص النطق حروف النص."),
    _f("TTS_PAUSAL_ENDINGS_FAILED", "prepare", "tts_text", VALIDATION, AUTO_RECOVERABLE, True,
       "Sentence-final words were vocalized; the next run repeats text preparation.",
       detail_ar="فشل ضبط نهايات الجمل للراوي."),
    _f("TTS_CHUNKING_FAILED", "synthesize", "tts_chunking", INVARIANT, CODE_DEFECT, False,
       "A sentence exceeds the TTS chunk limit and cannot be split; the chunker or text needs a code fix.",
       detail_ar="جملة أطول من حد مقطع الصوت."),
    _f("TTS_HARD_GATE_FAILED", "prepare", "tts_gate", VALIDATION, CODE_DEFECT, False,
       "A required text gate did not pass before TTS; the gate order or data needs a code fix.",
       detail_ar="لم تكتمل بوابات النص المطلوبة قبل الصوت."),
    _f("TTS_WAITING_FOR_QUOTA", "synthesize", "tts", QUOTA_WAIT, AUTO_RECOVERABLE, True,
       "Narrator quota is used up; finished chunks are saved and the next run resumes.",
       planned_wait=True, waiting_for_quota=True, detail_ar="انتهت حصة الراوي لليوم."),
    _f("WAITING_FOR_LLM_QUOTA", None, "llm_router", QUOTA_WAIT, AUTO_RECOVERABLE, True,
       "Every approved writing model is rate limited; the episode is saved and the next run resumes.",
       planned_wait=True, waiting_for_quota=True, detail_ar="حصة نماذج الكتابة مستنفدة مؤقتًا."),
    _f("AUDIO_QC_FAILED", "synthesize", "audio_qc", INVARIANT, CODE_DEFECT, False,
       "Text and audio chunk sequences disagree; this is an invariant violation.",
       detail_ar="عدم تطابق بين مقاطع النص والصوت."),
    _f("IMAGE_ACCURACY_GATE_FAILED", "background", "historical_accuracy_gate", VALIDATION, AUTO_RECOVERABLE, True,
       "The generated background failed the historical accuracy gate; the next run regenerates it.",
       detail_ar="لم تجتز الصورة بوابة الدقة التاريخية."),
    _f("THUMBNAIL_TEXT_FAILED", "background", "thumbnail_text", INVARIANT, CODE_DEFECT, False,
       "The thumbnail text renderer could not draw the text.",
       detail_ar="تعذّر رسم نص الصورة المصغرة."),
    _f("THUMBNAIL_TYPOGRAPHY_FAILED", "background", "thumbnail_typography", INVARIANT, CODE_DEFECT, False,
       "The thumbnail typography renderer could not lay out the text.",
       detail_ar="تعذّر تخطيط نص الصورة المصغرة."),
    _f("CLOUDFLARE_NETWORK_ERROR", "background", "image_provider", TRANSIENT, AUTO_RECOVERABLE, True,
       "The image provider was unreachable; the next run retries.",
       detail_ar="تعذّر الاتصال بمزود الصور."),
    _f("CLOUDFLARE_INVALID_RESPONSE", "background", "image_provider", TRANSIENT, AUTO_RECOVERABLE, True,
       "The image provider returned an unusable response; the next run retries.",
       detail_ar="أعاد مزود الصور استجابة غير صالحة."),
    _f("CLOUDFLARE_CREDENTIALS_MISSING", "background", "image_provider", CONFIGURATION, OWNER_ACTION, False,
       "Add CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to the secrets.", owner_action_required=True,
       detail_ar="بيانات اعتماد مزود الصور غير مضبوطة.",
       action_ar="أضف CLOUDFLARE_ACCOUNT_ID وCLOUDFLARE_API_TOKEN إلى أسرار المستودع."),
    _f("IMAGE_FREE_CAPACITY_EXHAUSTED", "background", "image_provider", COST_BLOCKED, AUTO_RECOVERABLE, True,
       "Free image capacity is exhausted; no paid provider was used. The next run retries.",
       cost_blocked=True, detail_ar="السعة المجانية لمزودي الصور مستنفدة؛ لم يُستخدم أي مزود مدفوع."),
    _f("VERCEL_FREE_QUOTA_EXHAUSTED", "background", "thumbnail_provider", QUOTA_WAIT, AUTO_RECOVERABLE, True,
       "The Vercel AI Gateway free credit is used up; no paid fallback was used and the background is kept. "
       "The next run generates only the thumbnail.",
       planned_wait=True, waiting_for_quota=True, cost_blocked=True,
       detail_ar="انتهى الرصيد المجاني في Vercel AI Gateway؛ لم يُستخدم أي بديل مدفوع."),
    _f("VERCEL_KEY_MISSING", "background", "thumbnail_provider", CONFIGURATION, OWNER_ACTION, False,
       "AI_GATEWAY_API_KEY is not set; add it with the local setup page.", owner_action_required=True,
       detail_ar="مفتاح Vercel AI Gateway غير مضبوط.",
       action_ar="افتحي صفحة إعداد Vercel المحلية وأدخلي المفتاح ثم احفظيه."),
    _f("VERCEL_AUTH_FAILED", "background", "thumbnail_provider", CONFIGURATION, OWNER_ACTION, False,
       "Vercel AI Gateway rejected the key; replace AI_GATEWAY_API_KEY.", owner_action_required=True,
       detail_ar="رفض Vercel AI Gateway المفتاح.",
       action_ar="أدخلي مفتاحًا صحيحًا من صفحة إعداد Vercel المحلية."),
    _f("VERCEL_MODEL_UNAVAILABLE", "background", "thumbnail_provider", CONFIGURATION, OWNER_ACTION, False,
       "openai/gpt-image-2.5-sunburst is not available to this key; no other model is used.", owner_action_required=True,
       detail_ar="نموذج Sunburst غير متاح لهذا المفتاح."),
    _f("VERCEL_REQUEST_FAILED", "background", "thumbnail_provider", TRANSIENT, AUTO_RECOVERABLE, True,
       "The Vercel AI Gateway request failed after the allowed attempts; the next run retries only the thumbnail.",
       detail_ar="تعذّر إنشاء الصورة المصغرة عبر Vercel AI Gateway."),
    _f("CANVA_THUMBNAIL_FAILED", "background", "thumbnail_provider", TRANSIENT, AUTO_RECOVERABLE, True,
       "The Canva thumbnail workflow failed; there is no fallback. The background is kept and the next run retries only the thumbnail.",
       detail_ar="تعذّر إنشاء الصورة المصغرة عبر Canva؛ لا يوجد بديل."),
    _f("CANVA_BACKGROUND_GENERATION_FAILED", "background", "thumbnail_provider", TRANSIENT, AUTO_RECOVERABLE, True,
       "Canva (Lucid Origin) could not generate the thumbnail background; the next run retries only the thumbnail.",
       detail_ar="تعذّر توليد خلفية الصورة المصغرة داخل Canva."),
    _f("CANVA_AUTOFILL_FAILED", "background", "thumbnail_provider", TRANSIENT, AUTO_RECOVERABLE, True,
       "Canva could not autofill the thumbnail template; the next run retries only the thumbnail.",
       detail_ar="تعذّر ملء قالب الصورة المصغرة في Canva."),
    _f("CANVA_EXPORT_FAILED", "background", "thumbnail_provider", TRANSIENT, AUTO_RECOVERABLE, True,
       "Canva could not export a valid 1280x720 thumbnail; the next run retries only the thumbnail.",
       detail_ar="تعذّر تصدير الصورة المصغرة من Canva."),
    _f("CANVA_AUTH_MISSING", "background", "thumbnail_provider", CONFIGURATION, OWNER_ACTION, False,
       "Canva is not connected: add CANVA_THUMBNAIL_WEBHOOK_URL and authorize the Canva credential in n8n.",
       owner_action_required=True, detail_ar="ربط Canva غير مكتمل.",
       action_ar="أضف CANVA_THUMBNAIL_WEBHOOK_URL وأكمل تفويض Canva داخل n8n."),
    _f("CANVA_TEMPLATE_INVALID", "background", "thumbnail_provider", CONFIGURATION, OWNER_ACTION, False,
       "The Canva brand template is missing or lacks the BACKGROUND/HOOK_TEXT fields.", owner_action_required=True,
       detail_ar="قالب Canva غير صالح.", action_ar="راجع قالب الصورة المصغرة في Canva."),
    _f("BLOCKED_COST_OR_FREE_LIMIT", None, "provider_quota", COST_BLOCKED, AUTO_RECOVERABLE, True,
       "A provider's free allowance is used up; no paid provider was used. The next run retries.",
       cost_blocked=True, detail_ar="انتهى الحد المجاني لأحد المزودين؛ لم يُستخدم أي مزود مدفوع."),
    _f("BLOCKED_COST", None, "cost_policy", COST_BLOCKED, OWNER_ACTION, False,
       "A paid or unapproved model was requested; fix the model/cost configuration.",
       owner_action_required=True, cost_blocked=True,
       detail_ar="طُلب نموذج أو مزود مدفوع/غير معتمد؛ لم يُستخدم أي مزود مدفوع.",
       action_ar="راجع إعدادات النموذج وسياسة التكلفة المجانية."),
    _f("GEMINI_AUTH_CONFIG_ERROR", None, "gemini_auth", CONFIGURATION, OWNER_ACTION, False,
       "Check the Gemini API key and project configuration.", owner_action_required=True,
       detail_ar="مفتاح Gemini أو إعدادات المشروع غير صالحة.",
       action_ar="راجع مفتاح Gemini وإعدادات المشروع."),
    _f("MISSING_CREDENTIAL", None, "credentials", CONFIGURATION, OWNER_ACTION, False,
       "Add the missing secret to the repository secrets.", owner_action_required=True,
       detail_ar="سرّ أو مفتاح مطلوب غير مضبوط.",
       action_ar="أضف السر الناقص إلى أسرار المستودع."),
    _f("ARCHIVE_UPLOAD_FAILED", "archive", "internet_archive", TRANSIENT, AUTO_RECOVERABLE, True,
       "Uploading the review copy to Internet Archive failed; the finished video is kept and the next run re-uploads.",
       detail_ar="فشل رفع نسخة المراجعة إلى Internet Archive."),
    _f("YOUTUBE_AUTH_FAILED", "publish", "youtube_auth", CONFIGURATION, OWNER_ACTION, False,
       "Re-authorize the YouTube OAuth credentials.", owner_action_required=True,
       detail_ar="رفض YouTube بيانات الاعتماد.",
       action_ar="أعد تفويض حساب YouTube (OAuth) وحدّث السر."),
    _f("YOUTUBE_OAUTH_PREFLIGHT_FAILED", "publish", "youtube_auth", CONFIGURATION, OWNER_ACTION, False,
       "Re-authorize YouTube with the required scopes.", owner_action_required=True,
       detail_ar="تعذّر التحقق من صلاحيات YouTube.",
       action_ar="أعد تفويض YouTube بالصلاحيات المطلوبة."),
    _f("YOUTUBE_PUBLISH_BLOCKED", "publish", "youtube_publish", VALIDATION, OWNER_ACTION, False,
       "Publishing was blocked by the packaging checks; regenerate the episode from review.",
       owner_action_required=True, detail_ar="منعت فحوص الحزمة النشر.",
       action_ar="أعد توليد الحلقة من رسالة المراجعة."),
    _f("HUMAN_APPROVAL_GATE_FAILED", "publish", "approval_gate", INVARIANT, OWNER_ACTION, False,
       "The archive item is not in a publishable review state.", owner_action_required=True,
       detail_ar="حالة عنصر الأرشيف لا تسمح بالنشر.",
       action_ar="تأكد أن الحلقة بحالة مراجعة معلّقة ثم أعد الاعتماد."),
    _f("CONSISTENCY_CHECK_FAILED", "publish", "approval_state", INVARIANT, CODE_DEFECT, False,
       "Local and remote approval state disagree.", detail_ar="تعارض بين الحالة المحلية وحالة الأرشيف."),
    _f("PREFLIGHT_VALIDATION_FAILED", "publish", "publish_preflight", VALIDATION, OWNER_ACTION, False,
       "Archive metadata failed the publish preflight; regenerate the episode from review.",
       owner_action_required=True, detail_ar="لم تجتز بيانات الحلقة فحص ما قبل النشر.",
       action_ar="أعد توليد الحلقة من رسالة المراجعة."),
    _f("RESEARCH_TOPIC_ALIGNMENT_INSUFFICIENT", "research_alignment", "research_alignment_gate", VALIDATION,
       OWNER_ACTION, False,
       "The saved sources do not support a documented angle; the episode is checkpointed. Review the topic.",
       owner_action_required=True,
       detail_ar="المصادر المحفوظة لا تدعم زاوية موثقة لهذه الحلقة؛ الحلقة محفوظة ولم تُخترع تفاصيل.",
       action_ar="راجع موضوع الحلقة أو اطلب استبداله؛ لا تُكمل قصة غير موثقة."),
    _f("RESEARCH_TOPIC_ALIGNMENT_FAILED", "research_alignment", "research_alignment_gate", VALIDATION,
       AUTO_RECOVERABLE, True,
       "The alignment check returned no usable verdict; the next run repeats it from the saved research.",
       detail_ar="لم تُرجع بوابة المواءمة حكمًا صالحًا."),
    _f("CHILD_PROCESS_FAILED", None, None, UNCLASSIFIED, None, False,
       "A child process failed without recording its own error; open the run log.", wrapper=True,
       detail_ar="فشلت عملية فرعية دون تسجيل سبب محدد."),
])

# Failures that carry no ``CODE:`` prefix but are still precisely recognisable.
_MISSING_CREDENTIAL = re.compile(
    r"(?i)\b[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|ACCOUNT_ID|_URL)\b[^.\n]*\b(?:is|are) (?:missing|required)\b"
    r"|credentials? (?:are|is) (?:not configured|required)"
    r"|missing youtube credential fields"
)
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?i)generated story is too short"), "STORY_TOO_SHORT"),
    (re.compile(r"(?i)invalid_grant|unauthorized_client|invalid_client|youtube.*(?:401|403)|(?:401|403).*youtube"), "YOUTUBE_AUTH_FAILED"),
    (re.compile(r"(?i)internet archive (?:upload|delete|metadata update) failed|archive upload is missing files|remote checksum"), "ARCHIVE_UPLOAD_FAILED"),
    (re.compile(r"(?i)^command failed \(\d+\)"), "CHILD_PROCESS_FAILED"),
)
_PREFIX = re.compile(r"^\s*([A-Z][A-Z0-9_]{5,})\b\s*(?::|$)")
_QUOTA_CODES = ("TTS_WAITING_FOR_QUOTA", "WAITING_FOR_LLM_QUOTA")
_SKIPPED_FRAMES = {"run_summary.py", "failure_registry.py", "error_classifier.py"}


def extract_error_code(exc: BaseException) -> str:
    """The error code of ``exc``: a known ``CODE:`` prefix, never a keyword guess."""
    text = str(exc)
    if type(exc).__name__ == "LlmQuotaWaiting":
        return "WAITING_FOR_LLM_QUOTA"
    match = _PREFIX.match(text)
    prefix = match.group(1) if match else ""
    if prefix.startswith("OUTLINE_GENERATION_FAILED") and prefix not in REGISTRY:
        prefix = "OUTLINE_GENERATION_FAILED"
    quota = next((code for code in _QUOTA_CODES if code in text.upper()), "")
    if quota and prefix not in ("BLOCKED_COST", "BLOCKED_COST_OR_FREE_LIMIT"):
        return quota  # a wrapper such as GROUNDED_RESEARCH_UNAVAILABLE around a quota wait
    if prefix in REGISTRY:
        return prefix
    if _MISSING_CREDENTIAL.search(text):
        return "MISSING_CREDENTIAL"
    for pattern, code in _PATTERNS:
        if pattern.search(text):
            return code
    return prefix  # an unregistered code is still reported as written


def repo_location(exc: BaseException) -> dict:
    """Deepest traceback frame inside this repository: relative file, function, line."""
    best = None
    for frame in traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []:
        try:
            path = Path(frame.filename).resolve()
            relative = path.relative_to(ROOT)
        except (ValueError, OSError):
            continue
        if path.name in _SKIPPED_FRAMES or any(part in (".venv", "site-packages", "venv") for part in relative.parts):
            continue
        best = (relative.as_posix(), frame.name, frame.lineno)
    if not best:
        return {"repo_file": None, "repo_function": None, "repo_line": None}
    return {"repo_file": best[0], "repo_function": best[1], "repo_line": best[2]}


def _legacy(exc: BaseException) -> tuple[str, str | None, bool]:
    """Category, policy and retryability for an exception the registry does not know."""
    try:
        from error_classifier import ErrorCategory, classify_error
        category = classify_error(exc) if isinstance(exc, Exception) else ErrorCategory.PERMANENT
    except Exception:
        category = "PERMANENT"
    if category == "TRANSIENT_RETRYABLE":
        return TRANSIENT, AUTO_RECOVERABLE, True
    if category == "CONFIGURATION":
        return CONFIGURATION, OWNER_ACTION, False
    if category == "VALIDATION":
        return VALIDATION, AUTO_RECOVERABLE, True
    return UNCLASSIFIED, CODE_DEFECT, False


def diagnose(exc: BaseException, current_stage: str = "") -> dict:
    """Structured, secret-free diagnosis. Unknown failures stay honestly unclassified."""
    code = extract_error_code(exc)
    known = REGISTRY.get(code)
    location = {} if (known and known.wrapper) else repo_location(exc)
    tracked = current_stage if current_stage not in ("", "produce") else ""
    if known:
        # The stage tracked at run time is the truth; the registry fills in when it is only "produce".
        stage = tracked or known.stage or current_stage or None
        policy = known.policy
        result = {
            "failed_stage": stage, "failed_component": known.component, "error_code": known.code,
            "error_category": known.category, "retryable": known.retryable,
            "recovery_policy": policy, "auto_recovery_available": policy == AUTO_RECOVERABLE,
            "owner_action_required": known.owner_action_required,
            "recommended_action": known.recommended_action,
            "planned_wait": known.planned_wait, "waiting_for_quota": known.waiting_for_quota,
            "cost_blocked": known.cost_blocked, "known_failure": True,
            "immediate_retry": known.immediate_retry, "max_attempts": known.max_attempts,
        }
    else:
        category, policy, retryable = _legacy(exc)
        result = {
            "failed_stage": tracked or current_stage or None, "failed_component": None,
            "error_code": code or None, "error_category": category, "retryable": retryable,
            "recovery_policy": policy, "auto_recovery_available": policy == AUTO_RECOVERABLE,
            "owner_action_required": policy == OWNER_ACTION,
            "recommended_action": (
                "Unregistered failure: inspect the file and function below and the run log."
                if policy == CODE_DEFECT else None
            ),
            "planned_wait": False, "waiting_for_quota": False, "cost_blocked": False, "known_failure": False,
            "immediate_retry": category == TRANSIENT, "max_attempts": 3 if category == TRANSIENT else 2,
        }
    result.update({"repo_file": None, "repo_function": None, "repo_line": None}, **location)
    return result
