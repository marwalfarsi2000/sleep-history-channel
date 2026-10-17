# لن تُكمل هذه القصة — Cloud Production

نظام سحابي مجاني-أولًا ينتج حلقة تاريخية عربية طويلة للنوم، يرفع نسخة مراجعة متحققة إلى Internet Archive، ثم ينتظر قرارًا صريحًا من OWNER عبر Telegram قبل أي نشر على YouTube.

## المسار

`Telegram أو الجدول اليومي → GitHub Actions → بحث موثق وبوابات جودة → Fact Check وتصحيح وتحقق ثانٍ → تشكيل ونطق → Gemini TTS وAudio QC → صورة خلفية وتدقيق تاريخي → تركيب النص العربي على المصغّرة بـPython → FFmpeg وVideo QC → رفع نسخة المراجعة والتحقق منها على Internet Archive → REVIEW_PENDING → قرار OWNER → نشر YouTube الآمن → PUBLISHED`

الحالات المعتمدة (`scripts/state_machine.py`): `GENERATING → MEDIA_READY → REVIEW_PENDING → APPROVED_FOR_PUBLISH → PUBLISHING → PUBLISHED`، إضافةً إلى `REJECTED` و`FAILED_RETRYABLE`.

Internet Archive هو مخزن نسخة المراجعة: يُرفع إليه الفيديو المتحقق **قبل** الموافقة. بوابة OWNER تمنع إتمام النشر على YouTube فقط، ولا تمنع إنشاء نسخة المراجعة.

يُراجع النص مشكولًا بالكامل، ثم تُحذف آليًا علامات التشكيل من آخر كلمة في كل جملة داخل نسخة TTS فقط لتطبيق الوقف العربي الطبيعي ومنع نطق حركات الإعراب أو التنوين في نهاية الجملة.

## قرارات المراجعة

تُنفَّذ عبر `.github/workflows/review.yml` (`python scripts/cloud_pipeline.py <action> --identifier <internet-archive-id>`):

- **publish (اعتماد ونشر):** يتحقق من أن عنصر الأرشيف في `REVIEW_PENDING`، ثم يشغّل مسار النشر الآمن على YouTube.
- **reject (رفض):** يحذف عنصر المراجعة من Internet Archive ويضع الحالة `REJECTED`، ولا يُنشر شيء على YouTube.
- **regenerate (إعادة التوليد):** ينزّل ملفات عنصر المراجعة ويعيد فقط المراحل غير الصالحة أو المتغيرة، ثم يرفع نسخة مراجعة جديدة.
- **notify-existing-review:** يعيد إرسال طلب المراجعة إلى Telegram لعنصر موجود في `REVIEW_PENDING` دون إعادة إنتاج.

الأمر `resume-review` في workflow الإنتاج يكمل التحقق من رفع Internet Archive وإرسال المراجعة لحلقة محفوظة.

## النشر الآمن على YouTube

الإعداد `youtube_publish_mode` في `config/settings.json`:

- `private_verify_public` (الافتراضي): يرفع الفيديو خاصًا، ينتظر معالجة YouTube، ثم يحوّله علنيًا.
- `approved_direct_public`: يرفعه علنيًا مباشرة بعد الموافقة.

قبل أي `videos.insert` يُتحقق من نطاقات OAuth الفعلية للتوكن؛ إذا نقصت الصلاحية يُرجَع `OWNER_ACTION_REQUIRED` دون رفع أي ملف. عند الاستئناف يُعاد استخدام فيديو YouTube الموجود بدل رفع نسخة ثانية.

## التشغيل

- يوميًا عبر `.github/workflows/production.yml` (`cron: 30 8 * * *` بتوقيت UTC، أي 12:30 بتوقيت دبي، بعد إعادة تعيين حصة Gemini اليومية)، ويدويًا بـ`workflow_dispatch`.
- عند الطلب من Telegram: `/new` أو `فيديو جديد` أو `حلقة جديدة` (`cloudflare/telegram-worker.js`).
- المراجعة والنشر والرفض وإعادة التوليد عبر `.github/workflows/review.yml`.
- كلا workflow يشتركان في `concurrency.group = sleep-history-production` فلا يعملان معًا.
- `.github/workflows/model-health.yml` يفحص النماذج المهيأة شهريًا.
- لا يعتمد التشغيل على اللابتوب بعد النشر السحابي.

## البيانات والحالة

- `main` للكود والإعدادات فقط. حالة التشغيل المتغيرة (سجل الموضوعات، فهرس الأرشيف، حالات الحلقات، حصص TTS) تُحفظ في فرع `automation-state` عبر git plumbing (`scripts/state_store.py`) دون تبديل فرع العمل، ولا يدفع أي تشغيل بيانات تشغيل إلى `main`.
- يبدأ كل workflow بـ`state_store.py hydrate` وينتهي بـ`scripts/git_sync.py` الذي يحفظ الحالة في `automation-state`.
- لكل حلقة `episode_id` ثابت بصيغة `ep_YYYYMMDD_<slug>_<hash>`، مستقل عن معرّف Internet Archive واسم مجلد القصة، ويُستخدم في سجل الحالة وفهرس الأرشيف وبيانات الحلقة.
- يكتب كل تشغيل ملخصًا تشخيصيًا في `.runtime/run_summary.json` (المرحلة الحالية، المرحلة الفاشلة، السبب، المراحل المعاد استخدامها أو المعاد توليدها، عدادات الاستدعاءات الخارجية بلا أسرار)، ويقرؤه `scripts/notify_failure.py` أولًا قبل تحليل السجلات.
- بعد فشل التشغيل تُحفظ السجلات ونقاط الاستئناف كـGitHub Actions artifact باسم `sleep-history-failure-<run_id>` لمدة 7 أيام (مع استبعاد ملفات الفيديو والصوت النهائية).
- الأسرار محفوظة في GitHub Actions Secrets وCloudflare Worker Secrets فقط.
- ملف `.env` وملفات n8n المحلية ومجلدات القصص والوسائط مستبعدة من Git.

## الصورة المصغّرة وإعادة التوليد الانتقائية

- مولّدات الصور ترسم **الخلفية فقط** بلا أي نص أو شعار؛ النص العربي الدقيق (2–4 كلمات) يضيفه Python لاحقًا.
- مرحلتان مستقلتان: `background` (`10_background.png`) و`thumbnail_overlay` (`11_thumbnail_base.jpg`). تغيّر نص المصغّرة أو نمط التركيب يعيد المصغّرة فقط دون TTS أو الخلفية أو الفيديو.
- حقول تصميم المصغّرة (`scene_type` و`focal_subject` و`composition` و`lighting` و`environment`) تُحفظ في بيانات الحلقة وفهرس الأرشيف وتُستخدم لتجنب التكرار مع آخر 10 حلقات منشورة.
- عنصر المراجعة على Internet Archive هو مخزن الوسائط الدائم: يحوي أيضًا `05b_story_tts_normalized.md` و`09_image_source.md` و`10_background.png` و`12_audio_narration.mp3` و`12_audio_full.mp3` و`13_audio_review.opus` و`14_video_final.mp4` و`17_status.md` و`logs/stage_manifest.json`، فيستعيدها runner جديد ويعيد استخدام المراحل الصالحة بعد فحص البصمات. العناصر القديمة التي تفتقر لبعضها تُعاد مراحلها اللازمة فقط.

## الاسترجاع بعد النشر

كل حلقة لها معرّف Internet Archive ثابت ورابط صفحة دائم. يمكن تنزيل الملف الأصلي من صفحة العنصر أو عبر:

```text
https://archive.org/download/<identifier>/14_video_final.mp4
```

حتى إذا اختفت قناة YouTube تبقى النسخة المستقلة في Internet Archive ما دامت لا تخالف شروط الخدمة.

## التشغيل اليدوي للمطور

```bash
python -m pip install -r requirements.txt
PYTHONPATH=scripts python -m pytest scripts/tests/
python scripts/cloud_pipeline.py produce
```

المتغيرات المطلوبة موثقة بأسمائها فقط في `.env.example`. لا تضع قيمًا حقيقية في المستودع.

## ضمان التكلفة

التصميم يستخدم الحصص المجانية فقط (`cost_policy: free_or_preapproved_only`). عند انتهاء حصة مزود ما يجب أن يفشل التشغيل بوضوح (`BLOCKED_COST`)، لا أن ينتقل تلقائيًا إلى خدمة مدفوعة. راقب حدود GitHub Actions وGemini وCloudflare لأنها حصص مجانية وليست استخدامًا غير محدود.
