# لن تُكمل هذه القصة — Cloud Production

نظام سحابي مجاني-أولًا ينتج حلقة تاريخية عربية طويلة، يرفع ملف MP4 الكامل إلى Internet Archive للمراجعة، ثم ينتظر قرارًا صريحًا من Telegram قبل النشر إلى YouTube.

## المسار

`Telegram أو الجدول اليومي → GitHub Actions → بحث ونص وتشكيل → Gemini TTS → Pink Noise 15% → صورة → FFmpeg → Internet Archive → مراجعة Telegram`

- **اعتماد ونشر:** ينزّل GitHub Actions الملف الكامل من Internet Archive ويرفعه إلى YouTube، ويبقي النسخة المؤرشفة.
- **رفض:** يحذف ملفات الحلقة المرفوضة من Internet Archive دون نشر.
- **إعادة التوليد:** يسترجع نص الحلقة وبياناتها من Internet Archive ويعيد الصوت والصورة والفيديو ثم يرسل رابط المراجعة نفسه.
- لا يعتمد التشغيل على اللابتوب بعد النشر السحابي.

## التشغيل

- يوميًا الساعة `02:00 Asia/Dubai` عبر `.github/workflows/production.yml`.
- عند الطلب من Telegram: `/new` أو `فيديو جديد` أو `حلقة جديدة`.
- المراجعة والنشر والحذف عبر `.github/workflows/review.yml`.
- لا توجد خطوة تنشر على YouTube قبل الضغط على `🚀 اعتماد ونشر`.

## البيانات والأسرار

- ملفات الفيديو لا تُحفظ في GitHub.
- الأسرار محفوظة في GitHub Actions Secrets وCloudflare Worker Secrets فقط.
- ملف `.env` وملفات n8n المحلية ومجلدات القصص والوسائط مستبعدة من Git.
- `config/archive_index.json` فهرس نصي صغير للروابط والبصمات والحالات، وليس حزمة لإعادة بناء الفيديو.

## الاسترجاع

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

