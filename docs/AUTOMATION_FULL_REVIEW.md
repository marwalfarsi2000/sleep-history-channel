# مراجعة الأتمتة الحالية — قناة «لن تُكمل هذه القصة»

آخر تحديث: 2026-10-18 (Asia/Dubai)

هذا المستند وصفي فقط وليس مصدر الحقيقة التنفيذية؛ المرجع الدقيق هو الكود في `scripts/` وملفات `.github/workflows/` و`config/`. لا يحتوي على قيم أسرار ولا على ملفات القصص أو الوسائط.

## 1. نقاط التشغيل

### الإنتاج اليومي أو اليدوي

- الملف: `.github/workflows/production.yml`
- تشغيل يدوي بواسطة `workflow_dispatch`.
- تشغيل مجدول بواسطة GitHub Actions عند `30 8 * * *` بتوقيت UTC (12:30 بتوقيت دبي، بعد إعادة تعيين حصة Gemini اليومية).
- يمنع تشغيل نسختين متزامنتين بواسطة `concurrency.group = sleep-history-production`، وهي المجموعة نفسها التي يستخدمها `review.yml`، فلا يعمل الإنتاج والمراجعة معًا.
- يبدأ بخطوة `state_store.py ensure` ثم `hydrate` لاستعادة حالة التشغيل من فرع `automation-state`، وينتهي بـ`scripts/git_sync.py` الذي يحفظ الحالة فيه.
- مهلة التشغيل: 355 دقيقة.
- الأمر الرئيسي: `python scripts/cloud_pipeline.py produce`.
- وضع استئناف الأرشفة والمراجعة فقط: `python scripts/cloud_pipeline.py resume-review`.
- يوجد workflow ثالث `model-health.yml` يفحص النماذج المهيأة شهريًا (`0 6 1 * *`).

### قرار المراجعة

- الملف: `.github/workflows/review.yml`
- الأوامر المتاحة: `publish` أو `reject` أو `regenerate` أو `notify-existing-review` (يعيد إرسال طلب المراجعة لعنصر في `REVIEW_PENDING`).
- يبدأ بـ`state_store.py ensure` و`hydrate` وينتهي بـ`git_sync.py`، ويحفظ `cloud_pipeline.py` حالة الحلقة في `automation-state` بعد كل قرار.
- التنفيذ: `python scripts/cloud_pipeline.py <action> --identifier <internet-archive-id>`.
- لا يتم النشر إلى YouTube قبل اختيار `publish`؛ أما نسخة المراجعة فتكون قد رُفعت إلى Internet Archive قبل ذلك.

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
   - `background`: توليد صورة الخلفية فقط (`10_background.png`) بلا أي نص والتحقق منها تاريخيًا.
   - `thumbnail_overlay`: إضافة النص العربي الدقيق للمصغّرة بواسطة Python (`11_thumbnail_base.jpg`).
   - `render`: إنشاء الفيديو بواسطة FFmpeg.
   - `validate`: فحص الملفات، المدة، الترميز، الدقة، الصوت والتشكيل.
   - `package`: تجهيز صفحة وملفات المراجعة.
6. رفع ملفات الحلقة إلى Internet Archive عبر `scripts/archive_storage.py`.
7. انتظار ظهور الفيديو فعليًا في Internet Archive والتحقق من الحجم والرابط.
8. تحديث الحالة إلى `REVIEW_PENDING` (مرت الحلقة قبلها بـ`GENERATING` ثم `MEDIA_READY`).
9. إرسال رابط الفيديو وصفحة الأرشيف وأزرار القرار إلى Telegram.
10. عند فشل أي خطوة، إرسال المرحلة والسبب إلى Telegram (من `.runtime/run_summary.json` أولًا ثم السجلات) وحفظ checkpoint تشخيصي لمدة 7 أيام.

## 2.1 حالة التشغيل ومعرّف الحلقة

- `main` للكود والإعدادات فقط؛ حالة التشغيل في فرع `automation-state` وتُقرأ وتُكتب عبر git plumbing (fetch وhash-object وmktree وcommit-tree ثم push لـcommit مباشر) دون تبديل فرع العمل أو تعديل شجرة العمل.
- لا يدفع أي تشغيل بيانات تشغيل إلى `main`.
- `episode_id` بصيغة `ep_YYYYMMDD_<slug>_<hash>` هو الهوية الثابتة للحلقة، مستقل عن معرّف Internet Archive واسم مجلد القصة، ويُخزَّن في بيانات الحلقة وسجل الحالة وفهرس الأرشيف وبيانات عنصر الأرشيف.
- المراحل المنفصلة `background` و`thumbnail_overlay` لها بصمات إدخال مستقلة في `logs/stage_manifest.json`؛ تغيّر نص المصغّرة أو نمط التركيب يعيد المصغّرة فقط.
- عنصر المراجعة على Internet Archive يحفظ إضافةً إلى ملفات الحلقة: `05b_story_tts_normalized.md` و`09_image_source.md` و`10_background.png` و`12_audio_narration.mp3` و`12_audio_full.mp3` و`13_audio_review.opus` و`14_video_final.mp4` و`17_status.md` و`logs/stage_manifest.json`.
- حقول تصميم المصغّرة (`thumbnail_scene_type` و`thumbnail_focal_subject` و`thumbnail_composition` و`thumbnail_lighting` و`thumbnail_environment`) تُستخدم لتجنب تكرار آخر 10 حلقات منشورة.

## 3. قواعد إنتاج القصة

- المرجع الكامل الإلزامي: `config/master_production_rules.md` (63 قسمًا مرقمًا).
- `scripts/generate_daily.py` يقرأ هذا الملف ويحقن الأقسام المناسبة فقط في كل طلب لتقليل التوكن:
  - اختيار الموضوع: الأقسام 2–16.
  - البحث: الأقسام 18–21.
  - المخطط: الأقسام 17 و22.
  - كتابة الفصول: الأقسام 2–7 و17 و23.
  - العنوان: القسم 44.
  - حزمة YouTube والمصغّرة: القسم 63 (`config/youtube_packaging_rules.md`).
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

- نموذج الكتابة الأساسي: `gemini-3.5-flash-lite` (`llm_model`).
- النماذج الاحتياطية بالترتيب (`llm_fallback_models`): `gemini-3.1-flash-lite` ثم `gemini-3.8-flash` ثم `gemini-3.6-flash` ثم `gemini-3-flash-preview`.
- سياسة التكلفة: `free_or_preapproved_only`؛ لا انتقال تلقائي إلى خدمة مدفوعة.
- مزود الصوت: Gemini TTS.
- نموذج الصوت الأساسي: `gemini-3.8-flash-tts`.
- نموذج الصوت الاحتياطي: `gemini-3.8-flash-lite-tts`، ثم Google Cloud TTS (`ar-XA-Wavenet-B` ثم `ar-XA-Standard-B`) ضمن سقوف شهرية مجانية.
- سرعة التوليد والمعالجة اللاحقة: `1.0`؛ لا يوجد تبطيء إلى 0.8.
- التشكيل: كامل في نسخة المراجعة، مع مرورتي مراجعة. في نسخة TTS النهائية تُحذف آليًا جميع علامات التشكيل من آخر كلمة في كل جملة لتطبيق الوقف العربي الطبيعي، وتفشل بوابة التحقق إذا بقي تشكيل في أي موضع نهاية جملة.
- مستوى Pink Noise: `0.15` (15%).
- مولد الصور الأساسي: Cloudflare Workers AI.
- نموذج الصورة: `@cf/black-forest-labs/flux-1-schnell` بعدد 4 خطوات.
- مزود الصورة الاحتياطي: Pollinations وفق وضع التكلفة المحدد. جميع المزودين يرسمون الخلفية فقط بلا نص أو شعار أو علامة مائية، والنص العربي يضيفه Python.
- مدة الفيديو المستهدفة: 3480–5400 ثانية (نحو 58–90 دقيقة).
- النشر يحتاج موافقة صريحة (`publish_requires: APPROVE_AND_PUBLISH`).
- وضع نشر YouTube (`youtube_publish_mode`): `private_verify_public` أو `approved_direct_public`.

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

توجد أسرار أخرى اختيارية مستخدمة في workflow الإنتاج (مثل `GOOGLE_CLOUD_TTS_API_KEY` ومفاتيح البحث)؛ المرجع الكامل خطوات `.github/workflows/production.yml`.

## 7. المراجعة والقرارات

### اعتماد ونشر

1. التحقق من أن حالة عنصر الأرشيف مسموحة للنشر (`REVIEW_PENDING` أو ما بعدها) وتطابق السجل المحلي، وأن بيانات الحزمة والمصغّرة اجتازت التحقق.
2. فحص صلاحيات OAuth الفعلية للتوكن حسب `youtube_publish_mode` **قبل** أي `videos.insert`. عند النقص يُرجَع `OWNER_ACTION_REQUIRED` دون رفع شيء وتبقى الحالة محفوظة.
3. النشر إلى YouTube بواسطة `scripts/youtube_publish.py`:
   - `private_verify_public`: رفع خاص، انتظار معالجة YouTube، ثم تحويله علنيًا.
   - `approved_direct_public`: رفع علني مباشر.
   - عند الاستئناف يُعاد استخدام الفيديو الموجود (من بيانات الأرشيف أو الحالة أو بحث العنوان) بدل رفع نسخة ثانية.
4. تحديث فهرس الأرشيف وبيانات Internet Archive وحالة الحلقة في `automation-state` إلى `PUBLISHED`.
5. إرسال روابط YouTube وInternet Archive إلى Telegram.

### رفض

1. حذف ملفات الحلقة المرفوضة من Internet Archive.
2. تغيير الحالة إلى `REJECTED`.
3. إرسال تأكيد إلى Telegram.

### إعادة التوليد

1. تنزيل النصوص والمصادر والبيانات وملفات الوسائط المتوفرة من عنصر Internet Archive، بما فيها `logs/stage_manifest.json`.
2. عدم إعادة اختيار الموضوع أو إعادة كتابة البحث والقصة.
3. التحقق من بصمات المراحل وإعادة استخدام الصالح منها (الصوت مثلًا)، وإعادة المرحلة المتأثرة فقط بواسطة `pipeline.py all`؛ العناصر القديمة التي تفتقر لبعض الملفات تُعاد مراحلها اللازمة فقط.
4. رفع النسخة الجديدة وإرسالها للمراجعة.

## 8. الاستئناف ومنع ضياع العمل

- تحفظ مراحل الموضوع والبحث والمخطط وكل فصل والوسائط كـcheckpoints.
- `restore_failure_checkpoint.py` يستعيد أحدث artifact صالح من التشغيل الفاشل.
- `generate_daily.py` يستأنف الحلقة الجزئية بدل اختيار موضوع جديد.
- `resume-review` يكمل تحقق Internet Archive وإرسال المراجعة دون إعادة الإنتاج.
- الاختبارات داخل `scripts/tests/` توثق حواجز الاستئناف والصوت والصورة والأرشفة والتنبيهات.

## 9. فشل التشغيل

- يكتب كل تشغيل `.runtime/run_summary.json` (الحلقة، المرحلة الحالية/الفاشلة، السبب الجذري، قابلية الإعادة، حجب التكلفة، إجراء OWNER المطلوب، المراحل المعاد استخدامها أو توليدها، وعدادات الاستدعاءات الخارجية فقط بلا أسرار).
- `scripts/notify_failure.py` يقرأ هذا الملخص أولًا، ويرجع إلى تحليل السجلات للحلقات القديمة أو عند غيابه، ويكوّن رسالة مختصرة.
- ترسل الرسالة تلقائيًا إلى Telegram مع رابط تشغيل GitHub Actions.
- تحفظ السجلات والـcheckpoints كـGitHub artifact باسم `sleep-history-failure-<run_id>` لمدة 7 أيام، مع استبعاد ملفات الفيديو والصوت النهائية.
- لا يؤدي نجاح الأمر تقنيًا وحده إلى اعتماد المنتج؛ التحقق النهائي إلزامي.

## 10. دليل الملفات التنفيذية داخل الحزمة

- `.github/workflows/production.yml`: تشغيل الإنتاج والاستئناف والفشل.
- `.github/workflows/review.yml`: اعتماد أو رفض أو إعادة توليد.
- `config/master_production_rules.md`: القواعد الإنتاجية الإلزامية (63 قسمًا).
- `config/settings.json`: النماذج والصوت والصورة والمدة والتكلفة.
- `config/youtube_description_template.txt`: وصف YouTube الثابت.
- `scripts/generate_daily.py`: الموضوع والبحث والقصة والعنوان والتدقيق.
- `scripts/pipeline.py`: التشكيل والصوت والصورة والفيديو والتحقق.
- `scripts/image_generate.py`: تكاملات توليد الصورة والتحقق منها.
- `scripts/cloud_pipeline.py`: المنسق السحابي ورسائل Telegram والقرارات.
- `scripts/archive_storage.py`: رفع Internet Archive والتحقق والحذف.
- `scripts/state_store.py` و`scripts/git_sync.py`: حالة التشغيل في فرع `automation-state` (hydrate/persist).
- `scripts/state_machine.py`: الحالات المعتمدة والانتقالات المسموحة.
- `scripts/episode_id.py`: معرّف الحلقة الثابت.
- `scripts/stage_manifest.py`: بصمات المراحل لإعادة الاستخدام الانتقائي.
- `scripts/thumbnail_design.py`: حقول تصميم المصغّرة ومنع التكرار.
- `scripts/restore_failure_checkpoint.py`: استعادة آخر checkpoint.
- `scripts/notify_failure.py`: رسالة الفشل التلقائية.
- `scripts/youtube_publish.py`: نشر YouTube الآمن بعد الموافقة (وضعا النشر، فحص OAuth، منع الرفع المكرر).
- `scripts/tests/`: الاختبارات الحالية لكل هذه المسارات.
- `requirements.txt`: الاعتماديات البرمجية.

## 11. حدود هذه الحزمة

- لا تتضمن قيم GitHub Secrets أو أي مفتاح API.
- لا تتضمن ملفات القصص أو الصوت أو الصور أو الفيديوهات المنتجة.
- بيانات التشغيل المتغيرة (`topics_history.json` و`archive_index.json` و`state.json` وحالات TTS) مصدرها الحقيقي فرع `automation-state`؛ النسخ الموجودة في `main` ليست إلا بذرة قديمة للقراءة الاحتياطية.
- استقبال رسائل Telegram والضغط على الأزرار قد يعتمد على تكامل خارجي؛ هذه الحزمة توثق وتضم كل ما هو موجود حاليًا داخل مستودع `sleep-history-channel`.
