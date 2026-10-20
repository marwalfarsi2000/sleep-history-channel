# مراجعة الأتمتة الحالية — قناة «لن تُكمل هذه القصة»

آخر مطابقة مع التنفيذ الفعلي: 2026-10-20. عند أي تعارض يُعتمد الكود وسجلات GitHub Actions وفرع `automation-state` على هذا المستند. لا تحتوي المستودعات على قيم الأسرار.

## 1. نقاط التشغيل

### الإنتاج — `.github/workflows/production.yml`

- `workflow_dispatch` يدويًا (مدخلات: `trigger_source`، `resume_archive_only`).
- مجدول: `30 8 * * *` UTC (12:30 دبي) هو التشغيل اليومي الوحيد الذي **يبدأ** موضوعًا جديدًا عند عدم وجود حلقة غير مكتملة.
- مجدولان إضافيان `30 14 * * *` و`30 20 * * *` يشغّلان `cloud_pipeline.py produce --resume-only`: يكملان حلقة غير مكتملة فقط (ولا ينتظران إن كانت الحلقة موقوفة حتى `blocked_until`)، ولا يبدآن موضوعًا جديدًا أبدًا. بلا حلقة غير مكتملة ينتهيان بنجاح دون عمل.
- `concurrency.group = sleep-history-production` بلا إلغاء، ومهلة 355 دقيقة، وPython 3.13.
- خطوة الفشل ترسل Telegram من `scripts/notify_failure.py` وترفع artifact التشخيص والـcheckpoint (`sleep-history-failure-<run>`) لمدة **30 يومًا**، ثم تحفظ الحالة على `automation-state` دائمًا.

### قرار المراجعة — `.github/workflows/review.yml`

`publish` / `reject` / `regenerate` / `notify-existing-review` بالأمر `python scripts/cloud_pipeline.py <action> --identifier <id>`. عند الفشل ترسل نفس رسالة التشخيص وترفع `sleep-history-review-failure-<run>` (30 يومًا). لا نشر إلى YouTube قبل `publish`.

### اختبارات PR — `.github/workflows/pr-test.yml`

يثبّت ffmpeg ويشغّل `PYTHONPATH=scripts python -m pytest scripts/tests/` (صلاحية `contents: read`، مهلة 20 دقيقة).

## 2. تسلسل الإنتاج

1. hydrate لحالة التشغيل من `automation-state`؛ تثبيت ffmpeg والاعتماديات.
2. `restore_failure_checkpoint.py`: ترحيل سجلات zombie مرة واحدة (`ABANDONED_LEGACY`)، ثم استعادة آخر artifact صالح للحلقة غير المكتملة، ثم ملء الناقص من checkpoint الدائم على `automation-state` (انتهاء artifact وحده لا يجعل الحلقة غير قابلة للاستئناف).
3. `cloud_pipeline.py produce` (مع استعادة تلقائية محدودة، القسم 6) ثم `generate_daily.py`:
   `topic_selection` ← `research` ← **`research_alignment`** ← `outline` ← `chapters` ← `quality_gates` ← `packaging`.
4. `pipeline.py all`: `prepare` ← `synthesize` ← `background` ← `render` ← `validate` ← `package`.
5. `archive`: رفع إلى Internet Archive والتحقق من الحجم/الـhash، ثم `publish` بعد الموافقة.
6. كل مرحلة تُسجَّل في `.runtime/run_summary.json` **قبل** تنفيذها.

## 3. اختيار الموضوع

يطلب النموذج 6–8 مرشحين (6 مفضّل). الحد الأدنى المقبول **4 مرشحين صالحين** (بعد دمج بنك الأفكار، دون المستخدم/المرفوض/المختار) من **4 عائلات** على الأقل؛ دونه `TOPIC_DISCOVERY_FAILED`. القاعدة 62 في `config/master_production_rules.md` تذكر هذا صراحة، وتختبر `test_master_production_rules.py` تطابقها مع الثوابت.

## 4. المخطط (Outline)

`GENERATE → REPAIR LOCALLY → TARGETED CORRECTION → CHECKPOINT`:

- 7–10 أقسام، 2–4 مشاهد لكل قسم، 5 مراحل بالترتيب، مجموع `target_words` لا يقل عن **5500** (لا يُخفَّض).
- `scripts/outline_repair.py` يصلح محليًا وحتميًا ما هو شكلي فقط: تحويل `target_words` النصية/العشرية إلى أعداد، رفع المجموع إلى 5500 بتوزيع متناسب بسقف 1400 للقسم، قصّ المشاهد الزائدة، دمج أقسام متجاورة من المرحلة نفسها، تقسيم قسم بأربعة مشاهد أو أكثر، إعادة تعيين المراحل بالموضع، وملء `main_idea` من `action` وانتقال محايد. لا يكتب أي محتوى تاريخي.
- ما لا يُصلَح محليًا يُرسل للنموذج مرة واحدة بكل أخطائه (`VALIDATION_ERRORS`) مع ملخص بنيوي مضغوط لا النص الخام. الحد الأعلى 4 استدعاءات للنموذج: أولي + تصحيحان + إعادة توليد أخيرة.
- لم يُعتمد structured output / `responseSchema`: لم يُتحقق منه مع النماذج المعتمدة، فبقي JSON mode + المحقق + طبقة الإصلاح.

## 5. بوابة مواءمة البحث مع الموضوع

`scripts/research_alignment.py` بعد البحث وقبل الـOutline: `PASS` / `REANCHOR_REQUIRED` / `RESEARCH_INSUFFICIENT`. عند عدم الدعم تُعاد الزاوية والعمود السردي والعناصر لنفس الحلقة اعتمادًا على ما تثبته المصادر فقط، مع بحث إضافي واحد محدود إن لزم؛ وإن لم يكفِ يتوقف التشغيل بـ`RESEARCH_TOPIC_ALIGNMENT_INSUFFICIENT` عند checkpoint قابل للاستئناف. الحقول المحفوظة: `research_topic_alignment_status`، `unsupported_topic_elements`، `reanchored_elements`، `alignment_reason`. عند تغيّر الزاوية يُحذف أي outline/فصول قديمة. تُتخطى البوابة للقصة المنتهية.

## 6. الاستعادة والاستئناف

- `AUTO_RECOVERABLE` يعني استعادة فعلية: `produce_with_recovery` يعيد التشغيل داخل الـjob نفسه حتى `MAX_AUTORECOVERY_ATTEMPTS = 2` (وحد المدخل في السجل) بتأخير 30 ثم 90 ثانية، ولا يعيد الأعمال المنجزة لأن `generate_daily.py` و`pipeline.py` يستأنفان من الـcheckpoint. شروطه: السجل يضع الخطأ `AUTO_RECOVERABLE` و`immediate_retry`، لا quota ولا cost-block ولا owner action، وcheckpoint محفوظ (أو مرحلة اختيار الموضوع)، وضمن 4 ساعات من بدء الـjob. كل محاولة تُسجَّل في `auto_recovery_log`.
- انتظار الحصة (`WAITING_FOR_LLM_QUOTA`, `TTS_WAITING_FOR_QUOTA`) لا يُنتظر داخل الـrunner: يحفظ checkpoint وينتهي، وتكمله تشغيلات الجدولة التالية (القسم 1).
- الأخطاء `OWNER_ACTION` و`CODE_DEFECT` لا تُعاد تلقائيًا.

## 7. الـCheckpoints

- artifact الفشل: 30 يومًا.
- نسخة نصية صغيرة دائمة على `automation-state/checkpoints/<story>.json` (الموضوع، البحث، المصادر، المخطط، الفصول، بيانات البوابات) تُحفظ بعد: topic_selection، research_alignment، outline، chapters، quality_gates وعند نهاية كل تشغيل. لا صوت ولا فيديو ولا صور (هذه تبقى في artifact أو Internet Archive).
- `run_summary.json`: `checkpoint_saved` يصبح `true` عند أول checkpoint فعلي، ومعه `last_completed_stage`، `resume_stage`، `checkpoint_location`، `checkpoint_artifact_id`، `checkpoint_source`، `checkpoint_created_at`، `reused_stages`، `regenerated_stages`.
- سجل `zombie` (مثل `dhofar-incense-network`) يُرحَّل مرة واحدة إلى `ABANDONED_LEGACY` مع `status_before_migration` و`migration_reason`؛ لا يُحذف التاريخ.

## 8. التشخيص

`scripts/failure_registry.py` (الرموز المعروفة: المرحلة، المكوّن، الفئة، السياسة) + `run_summary.json` (`failed_stage`, `failed_component`, `error_code`, `repo_file/function/line`, `recovery_policy`…) + رسائل Telegram: المرحلة والمكوّن والسبب وما حُفظ ومن أين سيكمل وهل يلزم إجراء. جملة «أرسلي هذه الرسالة إلى Codex» فقط لـ`CODE_DEFECT`.

## 9. الصوت (TTS)

Gemini TTS أساسي ثم نموذجه الاحتياطي ثم Google Cloud TTS ضمن سقوف شهرية في `config/settings.json`، بصوت راوٍ واحد للحلقة، بلا مدفوع/overage. المقاطع الناجحة تُحفظ مع hash لكل مقطع (`logs/tts_chunk_hashes.json`) ولا تُعاد دون تغيّر نصها. حصة Gemini اليومية → `TTS_WAITING_FOR_QUOTA` مع حفظ المقاطع. بوابات QC (تطابق النص، المدة 3480–5400 ثانية، عدد المقاطع) لم تُخفَّف.

Pink Noise: ملف صالح موجود ← وإلا يُولَّد محليًا بـffmpeg (`anoisesrc=color=pink`، 300 ثانية وتُكرَّر عند المزج) عبر `scripts/background_audio.py`. `PINK_NOISE_URL` اختياري ولا يلزم سر ولا استضافة.

## 10. الصور

الخلفية المرئية للفيديو `10_background.png` تُنشأ من مزود الصور المهيأ (Cloudflare Workers AI أساسًا، وPollinations احتياطيًا وفق سياسة السعة المجانية). عند رفض بوابة الدقة التاريخية تُعاد الخلفية وحدها على المزود نفسه بتعليمة تصحيحية حسب سبب الرفض، وتُسجَّل المحاولات في `logs/image_attempts.json` و`15_metadata.json`. انتهاء السعة المجانية → `IMAGE_FREE_CAPACITY_EXHAUSTED` والحلقة محفوظة.

الصورة المصغرة النهائية `11_thumbnail_base.jpg` مسار مستقل بالكامل: n8n/Canva يولّد الخلفية بـLucid Origin ثم يملأ قالب Canva الثابت `EAHYe4Y0z-s` بحقلَي `BACKGROUND` و`HOOK_TEXT` ويصدر 1280×720. لا يوجد Pillow overlay ولا Puter/Vercel/Sunburst fallback للإنتاج. يُحفظ `thumbnail_input_sha256` لهوية نص الـHOOK + البرومبت الفعلي + القالب + نموذج الخلفية + أبعاد الإخراج، حتى لا يعاد استخدام Thumbnail قديم بعد تغيّر أي مدخل.

## 11. Internet Archive

رفع بـchecksum (لا يعيد رفع ملف مطابق)، والتحقق من الحجم وsha256/md5. أخطاء 5xx/429/انتهاء المهلة أثناء الاستعلام عن العنصر أو HEAD تُعاد بتأخير محدود؛ 404 يعني «لم يظهر بعد» ويُنتظر.

عند رفض حلقة، الحذف لا يستخدم `internetarchive.delete(..., headers=...)` لأن هذا المسار غير متوافق مع `internetarchive==5.12.0`. بدلًا منه تُحذف فقط أسماء الملفات التي تملكها الأتمتة عبر `item.get_file(name).delete(cascade_delete=True, ...)`؛ مشتقات Archive.org والملفات غير المعروفة لا تُرسل للحذف مباشرة.

## 12. YouTube

لا نشر قبل `publish`. حماية التكرار: يُبحث عن `youtube_video_id` في metadata الأرشيف و`.runtime` وسجل المواضيع وفهرس الأرشيف و`automation-state`؛ وحفظ المعرّف فور `videos.insert` في Archive وautomation-state. YouTube لا يوفّر مفتاح idempotency، لذلك يُكتب قبل `insert` علامة `youtube_upload_status=inserting`؛ فإن وُجدت دون معرّف والتوكن بلا نطاق قراءة (لا يمكن البحث) يتوقف بـ`REAUTHORIZE_YOUTUBE_OAUTH_WITH_READ_SCOPE` بدل رفع نسخة ثانية. فشل رفع الصورة المصغرة بعد نجاح الفيديو لا ينشئ فيديو جديدًا: يُعاد المحاولة ويُسجَّل `youtube_thumbnail_status`.

## 13. الأسرار — الأسماء فقط (`.env.example`)

`GEMINI_API_KEY`, `GEMINI_TTS_VOICE_ID`, `GEMINI_TTS_VOICE_NAME`, `GOOGLE_CLOUD_TTS_API_KEY`, `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY`, `FIRECRAWL_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `POLLINATIONS_API_KEY`, `IAS3_ACCESS_KEY`, `IAS3_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`، و`PINK_NOISE_URL` (اختياري). يتحقق `test_env_and_workflows.py` أن كل سر في الـworkflows موثّق.

## 14. ملاحظات معروفة

- تحذير Node 20 في `actions/checkout@v4` و`actions/setup-python@v5` و`actions/upload-artifact@v4`: يعمل حاليًا (يُجبَر على Node 24)؛ لم تُرفع الإصدارات دون تحقق من الإصدار المستقر.
- PR #14 الخاص بمسار الطباعة العربية القديم بقي Legacy ولم يُلمس في هذه المراجعة؛ الإنتاج الحالي يعتمد Canva، لذلك لا ينبغي دمجه تلقائيًا دون مراجعة مستقلة.
