# مراجعة الأتمتة الحالية — قناة «لن تُكمل هذه القصة»

تاريخ التجميع: 2026-10-15 (Asia/Dubai)

هذا المستند خريطة مراجعة للنسخة الحالية. الملفات البرمجية الأصلية المرفقة معه داخل الحزمة هي المرجع التنفيذي الدقيق. لا تحتوي الحزمة على قيم الأسرار، ملفات `.env`، القصص المنتجة، الصوت، الصور أو الفيديوهات.

## 1. نقاط التشغيل

### الإنتاج اليومي أو اليدوي

- الملف: `.github/workflows/production.yml`
- تشغيل يدوي بواسطة `workflow_dispatch`.
- تشغيل مجدول بواسطة GitHub Actions عند `0 22 * * *` بتوقيت UTC.
- يمنع تشغيل نسختين متزامنتين بواسطة `concurrency.group = sleep-history-production`.
- مهلة التشغيل: 355 دقيقة.
- الأمر الرئيسي: `python scripts/cloud_pipeline.py produce`.
- وضع استئناف الأرشفة والمراجعة فقط: `python scripts/cloud_pipeline.py resume-review`.

### قرار المراجعة

- الملف: `.github/workflows/review.yml`
- الأوامر المتاحة: `publish` أو `reject` أو `regenerate`.
- التنفيذ: `python scripts/cloud_pipeline.py <action> --identifier <internet-archive-id>`.
- لا يتم النشر إلى YouTube قبل اختيار `publish`.

## 2. تسلسل الإنتاج الكامل

1. تثبيت Python 3.13 وFFmpeg واعتماديات `requirements.txt`.
2. استعادة أحدث checkpoint فاشل عبر `scripts/restore_failure_checkpoint.py`.
3. التأكد من وجود Pink Noise؛ يُستخدم الملف المحلي أو `PINK_NOISE_URL` عند غيابه.
4. تشغيل `scripts/generate_daily.py`:
   - استئناف الحلقة الجزئية إن وجدت.
   - اختيار موضوع غير مكرر من سجل `config/topics_history.json`.
   - تنفيذ بحث تاريخي بمصادر خارجية.
   - إنشاء مخطط الأقسام.
   - كتابة الفصول وحفظ كل فصل checkpoint.
   - إنشاء العنوان والوصف والمصادر والنص النظيف وبيانات الحلقة.
   - إنشاء تقرير التدقيق التاريخي.
5. تشغيل `python scripts/pipeline.py all <story-path>`:
   - `prepare`: التشكيل، التحقق اللفظي، تنظيف نسخة TTS وتقسيمها.
   - `tts`: توليد المقاطع الصوتية ودمجها.
   - مزج Pink Noise بالمستوى المحدد في الإعدادات.
   - `image`: توليد صورة الحلقة والتحقق منها.
   - `render`: إنشاء الفيديو بواسطة FFmpeg.
   - `validate`: فحص الملفات، المدة، الترميز، الدقة، الصوت والتشكيل.
   - `package`: تجهيز صفحة وملفات المراجعة.
6. رفع ملفات الحلقة إلى Internet Archive عبر `scripts/archive_storage.py`.
7. انتظار ظهور الفيديو فعليًا في Internet Archive والتحقق من الحجم والرابط.
8. تحديث الحالة إلى `pending_approval`.
9. إرسال رابط الفيديو وصفحة الأرشيف وأزرار القرار إلى Telegram.
10. عند فشل أي خطوة، إرسال المرحلة والسبب إلى Telegram وحفظ checkpoint تشخيصي لمدة 7 أيام.

## 3. قواعد إنتاج القصة

- المرجع الكامل الإلزامي: `config/master_production_rules.md`.
- `scripts/generate_daily.py` يقرأ هذا الملف ويحقن الأقسام المناسبة فقط في كل طلب لتقليل التوكن:
  - اختيار الموضوع: الأقسام 2–16.
  - البحث: الأقسام 18–21.
  - المخطط: الأقسام 17 و22.
  - كتابة الفصول: الأقسام 2–7 و17 و23.
  - العنوان: القسم 44.
  - التدقيق التاريخي: الأقسام 26–29.
- إذا كان قسم مطلوب غير موجود، يتوقف التنفيذ بدل المتابعة بقواعد ناقصة.

## 4. الملفات التي تُنتج لكل حلقة

- `01_title.txt`: العنوان.
- `02_description.txt`: وصف YouTube.
- `03_sources.md`: المصادر.
- `04_research_notes.md`: ملاحظات البحث.
- `05_story_clean.md`: النص النهائي للبشر.
- `06_story_tts_annotated.md`: النص المشكول والمراجع صوتيًا.
- `07_story_tts_api.md`: النص النهائي المرسل إلى TTS.
- `08_image_prompt_en.txt`: طلب الصورة.
- `09_image_source.md`: بيانات مصدر الصورة الحقيقي.
- `10_background.png`: صورة الفيديو.
- `11_thumbnail_base.jpg`: أساس الصورة المصغرة.
- `12_audio_narration.mp3`: صوت الراوي.
- `12_audio_full.mp3`: الصوت بعد مزج Pink Noise.
- `13_audio_review.opus`: نسخة مراجعة الصوت.
- `14_video_final.mp4`: الفيديو النهائي.
- `15_metadata.json`: الحالة والبيانات الفعلية.
- `16_fact_check.md`: تقرير التدقيق التاريخي.
- `17_status.md`: حالة الحلقة.
- `logs/`, `tts_chunks/`, `audio_chunks/`: checkpoints والسجلات والمقاطع.

## 5. الإعدادات الحالية

المرجع الدقيق: `config/settings.json`.

- نموذج كتابة القصة الأساسي: `gemini-3.1-flash-lite`.
- نموذج الكتابة الاحتياطي: `gemini-3.5-flash-lite`.
- مزود الصوت: Gemini TTS.
- نموذج الصوت الأساسي: `gemini-3.8-flash-tts`.
- نموذج الصوت الاحتياطي: `gemini-3.8-flash-lite-tts`.
- سرعة التوليد والمعالجة اللاحقة: `1.0`؛ لا يوجد تبطيء إلى 0.8.
- التشكيل: كامل في نسخة المراجعة، مع مرورتي مراجعة. في نسخة TTS النهائية تُحذف آليًا جميع علامات التشكيل من آخر كلمة في كل جملة لتطبيق الوقف العربي الطبيعي، وتفشل بوابة التحقق إذا بقي تشكيل في أي موضع نهاية جملة.
- مستوى Pink Noise: `0.15` (15%).
- مولد الصور الأساسي: Cloudflare Workers AI.
- نموذج الصورة: `@cf/black-forest-labs/flux-1-schnell` بعدد 4 خطوات.
- مزود الصورة الاحتياطي: Pollinations وفق وضع التكلفة المحدد.
- مدة الفيديو المستهدفة: 3480–5400 ثانية (نحو 58–90 دقيقة).
- النشر يحتاج موافقة صريحة.

## 6. الأسرار المطلوبة — الأسماء فقط

القيم محفوظة في GitHub Secrets ولا توجد في هذه الحزمة:

- `GEMINI_API_KEY`
- `GEMINI_TTS_VOICE_ID`
- `GEMINI_TTS_VOICE_NAME`
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- `POLLINATIONS_API_KEY`
- `IAS3_ACCESS_KEY`
- `IAS3_SECRET_KEY`
- `PINK_NOISE_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

## 7. المراجعة والقرارات

### اعتماد ونشر

1. تنزيل الفيديو النهائي من Internet Archive عند الحاجة.
2. النشر إلى YouTube بواسطة `scripts/youtube_publish.py`.
3. تحديث فهرس الأرشيف وبيانات Internet Archive.
4. إرسال روابط YouTube وInternet Archive إلى Telegram.

### رفض

1. حذف ملفات الحلقة المرفوضة من Internet Archive.
2. تغيير الحالة إلى `rejected_deleted`.
3. إرسال تأكيد إلى Telegram.

### إعادة التوليد

1. تنزيل النصوص والمصادر والبيانات الموجودة من Internet Archive.
2. عدم إعادة اختيار الموضوع أو إعادة كتابة البحث والقصة.
3. إعادة مراحل الوسائط اللازمة بواسطة `pipeline.py all`.
4. رفع النسخة الجديدة وإرسالها للمراجعة.

## 8. الاستئناف ومنع ضياع العمل

- تحفظ مراحل الموضوع والبحث والمخطط وكل فصل والوسائط كـcheckpoints.
- `restore_failure_checkpoint.py` يستعيد أحدث artifact صالح من التشغيل الفاشل.
- `generate_daily.py` يستأنف الحلقة الجزئية بدل اختيار موضوع جديد.
- `resume-review` يكمل تحقق Internet Archive وإرسال المراجعة دون إعادة الإنتاج.
- الاختبارات داخل `scripts/tests/` توثق حواجز الاستئناف والصوت والصورة والأرشفة والتنبيهات.

## 9. فشل التشغيل

- `scripts/notify_failure.py` يحدد آخر مرحلة ناجحة ويكوّن رسالة مختصرة.
- ترسل الرسالة تلقائيًا إلى Telegram مع رابط تشغيل GitHub Actions.
- تحفظ السجلات والـcheckpoints كـGitHub artifact لمدة 7 أيام.
- لا يؤدي نجاح الأمر تقنيًا وحده إلى اعتماد المنتج؛ التحقق النهائي إلزامي.

## 10. دليل الملفات التنفيذية داخل الحزمة

- `.github/workflows/production.yml`: تشغيل الإنتاج والاستئناف والفشل.
- `.github/workflows/review.yml`: اعتماد أو رفض أو إعادة توليد.
- `config/master_production_rules.md`: القواعد الستون كاملة.
- `config/settings.json`: النماذج والصوت والصورة والمدة والتكلفة.
- `config/youtube_description_template.txt`: وصف YouTube الثابت.
- `scripts/generate_daily.py`: الموضوع والبحث والقصة والعنوان والتدقيق.
- `scripts/pipeline.py`: التشكيل والصوت والصورة والفيديو والتحقق.
- `scripts/image_generate.py`: تكاملات توليد الصورة والتحقق منها.
- `scripts/cloud_pipeline.py`: المنسق السحابي ورسائل Telegram والقرارات.
- `scripts/archive_storage.py`: رفع Internet Archive والتحقق والحذف.
- `scripts/restore_failure_checkpoint.py`: استعادة آخر checkpoint.
- `scripts/notify_failure.py`: رسالة الفشل التلقائية.
- `scripts/youtube_publish.py`: نشر YouTube بعد الموافقة.
- `scripts/tests/`: الاختبارات الحالية لكل هذه المسارات.
- `requirements.txt`: الاعتماديات البرمجية.

## 11. حدود هذه الحزمة

- لا تتضمن قيم GitHub Secrets أو أي مفتاح API.
- لا تتضمن ملفات القصص أو الصوت أو الصور أو الفيديوهات المنتجة.
- لا تتضمن `config/topics_history.json` أو `config/archive_index.json` لأنها بيانات تشغيل متغيرة وليست قواعد أو أوامر.
- استقبال رسائل Telegram والضغط على الأزرار قد يعتمد على تكامل خارجي؛ هذه الحزمة توثق وتضم كل ما هو موجود حاليًا داخل مستودع `sleep-history-channel`.
