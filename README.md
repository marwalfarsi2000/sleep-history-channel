# لن تُكمل هذه القصة — Cloud Production

نظام سحابي مجاني-أولًا ينتج حلقة تاريخية عربية طويلة، يحفظها كحزمة مراجعة خاصة في GitHub Actions، ثم ينتظر قرارًا صريحًا من Telegram قبل رفعها إلى Internet Archive وYouTube.

## المسار

`Telegram أو الجدول اليومي → GitHub Actions → بحث موثق وبوابات جودة → Fact Check وتصحيح وتحقق ثانٍ → تشكيل ونطق → Gemini TTS وAudio QC → صورة وتدقيق تاريخي → FFmpeg وVideo QC → حزمة مراجعة خاصة → موافقة Marwa → Internet Archive وYouTube → تحقق نهائي`

- **اعتماد ونشر:** يستعيد GitHub Actions حزمة المراجعة الخاصة، ثم يرفع النسخة المتحققة إلى Internet Archive وYouTube ويتحقق من المخرج النهائي.
- **رفض:** يغيّر حالة الحلقة إلى `rejected` ولا يرفع أي ملف عام.
- **إعادة التوليد:** يستعيد حزمة المراجعة الخاصة ويعيد فقط المراحل غير الصالحة أو المتغيرة ثم ينشئ حزمة مراجعة جديدة.
- لا يعتمد التشغيل على اللابتوب بعد النشر السحابي.

## التشغيل

- يوميًا الساعة `12:30 Asia/Dubai` (بعد إعادة تعيين حصة Gemini اليومية) عبر `.github/workflows/production.yml`.
- عند الطلب من Telegram: `/new` أو `فيديو جديد` أو `حلقة جديدة`.
- المراجعة والنشر والرفض عبر `.github/workflows/review.yml`.
- لا توجد خطوة ترفع إلى Internet Archive أو YouTube قبل الضغط على `🚀 اعتماد ونشر`.

## البيانات والأسرار

- ملفات المراجعة تُحفظ مؤقتًا كـGitHub Actions Artifact خاص لمدة 30 يومًا، ولا تُحفظ في Git.
- الأسرار محفوظة في GitHub Actions Secrets وCloudflare Worker Secrets فقط.
- ملف `.env` وملفات n8n المحلية ومجلدات القصص والوسائط مستبعدة من Git.
- `config/archive_index.json` فهرس نصي صغير للروابط والبصمات والحالات، وليس حزمة لإعادة بناء الفيديو.

## الاسترجاع بعد النشر

كل حلقة لها معرّف Internet Archive ثابت ورابط صفحة دائم. يمكن تنزيل الملف الأصلي من صفحة العنصر أو عبر:

```text
https://archive.org/download/<identifier>/14_video_final.mp4
```

حتى إذا اختفت قناة YouTube تبقى النسخة المستقلة في Internet Archive ما دامت لا تخالف شروط الخدمة.

## التشغيل اليدوي للمطور

```bash
python -m pip install -r requirements.txt
python scripts/cloud_pipeline.py produce
```

المتغيرات المطلوبة موثقة بأسمائها فقط في `.env.example`. لا تضع قيمًا حقيقية في المستودع.

## ضمان التكلفة

التصميم يستخدم الحصص المجانية فقط. عند انتهاء حصة مزود ما يجب أن يفشل التشغيل بوضوح، لا أن ينتقل تلقائيًا إلى خدمة مدفوعة. راقب حدود GitHub Actions وGemini وCloudflare لأنها حصص مجانية وليست استخدامًا غير محدود.

