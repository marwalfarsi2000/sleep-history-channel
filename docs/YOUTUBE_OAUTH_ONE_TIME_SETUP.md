# ربط YouTube مرة واحدة — من الكمبيوتر، دون لصق Authorization Code

> هذا التدفق ينفذ على كمبيوتر المالك مرة واحدة. الإنتاج والنشر اللاحقان يظلان على GitHub Actions. لا يحتاج جهازك إلى البقاء مفتوحًا بعد الربط.

## ما أصلحناه

- ألغينا خانة `auth_code` من GitHub Actions؛ كانت تنقل رمز Google المؤقت في مدخل تشغيل غير مناسب للأسرار.
- أضفنا `scripts/youtube_oauth_local.py`: يفتح Google، ويستقبل الموافقة محليًا ويتحقق من `state` وPKCE، ثم يجدد الرمز، ويتأكد من القناة، ويحدّث أسرار GitHub عبر `gh`.
- أصبحت `.github/workflows/youtube-reauth.yml` أداة فحص اتصال باسم **YouTube Connection Check**، ولا ترفع أي فيديو.
- لا يُستخدم `GH_SECRETS_PAT` داخل GitHub Actions بعد ذلك.

## قبل التفويض

1. في Google Auth Platform > Audience تحقق من وضع **In production** لنفس المشروع الذي يملك OAuth Client. إذا ظل في Testing فقد تنتهي رموز التفويض بعد 7 أيام.
2. في Google Cloud > Clients استخدم **OAuth Client الموجود نفسه** الذي ستصدر ملف إعداداته. من قائمة العميل نزّل Client JSON عبر الخيار المخصص لذلك. إن لم يتوفر، جهّز ملف client JSON رسميًا من Google Cloud محليًا، ولا تحفظه في Git.
3. احفظ ملف client JSON خارج مجلد المستودع، مثل Downloads. لا تشاركه ولا تضفه إلى Git.
4. تأكد أن **YouTube Data API v3** مفعلة في المشروع الصحيح.
5. ثبت GitHub CLI إن لم يكن موجودًا، ثم نفّذ `gh auth login` بحساب `marwalfarsi2000`، وتأكد أن الوصول إلى مستودع `sleep-history-channel` متاح. لا تحتاج إنشاء PAT جديد داخل GitHub Secrets.
6. أغلق مؤقتًا الخدمة التي تشغل المنفذ 5679 إذا كانت n8n أو خدمة أخرى. أو أضف Redirect URI محليًا ثانيًا في Google Cloud، ونزّل Client JSON محدثًا لاستخدام `--redirect-uri`.

## تشغيل مرة واحدة

من مجلد المشروع، نفذ في الطرفية:

```powershell
python scripts/youtube_oauth_local.py --client-json "C:\Users\YOUR_USER\Downloads\client_secret.json"
```

إذا كان المنفذ 5679 مشغولًا ويحتوي ملف العميل الرسمي على Redirect URI مسجل آخر:

```powershell
python scripts/youtube_oauth_local.py --client-json "C:\Users\YOUR_USER\Downloads\client_secret.json" --redirect-uri "http://localhost:8765/callback"
```

- افتح شاشة Google، واختر الحساب الذي يملك **القناة الصحيحة**.
- راجع الصلاحيات المطلوبة (`youtube.upload` و`youtube.readonly`) ووافق إذا كان التطبيق ملكك.
- لا تنسخ `code=` من المتصفح. ستظهر صفحة نجاح محليًا تلقائيًا.
- سيظهر اسم القناة ومعرفها في الطرفية؛ اكتب `YES` **فقط إذا كانت القناة الصحيحة**.
- البرنامج يجدد Access Token للتحقق، ثم يخزن عبر GitHub CLI أربعة أسرار:
  `YOUTUBE_CLIENT_ID` و`YOUTUBE_CLIENT_SECRET` و`YOUTUBE_REFRESH_TOKEN` و`YOUTUBE_EXPECTED_CHANNEL_ID`.
- لا يكتب أي قيمة سرية في ملفات المستودع أو سجلات GitHub.

## تأكيد التشغيل السحابي

في GitHub > Actions > **YouTube Connection Check** > **Run workflow**.

النجاح المثبت يتطلب: تجديد الرمز + وجود صلاحيات القراءة والرفع + تطابق هوية القناة.
لا تعتمدي على نجاح شاشة Google وحده.

بعد نجاح الاختبار، الغي رمز GitHub المؤقت القديم `GH_SECRETS_PAT` من Personal access tokens، واحذفي السر الذي يحمل اسمه من المستودع إن لم يعد مستخدمًا.

إذا فشل Google بــ `invalid_grant` لا تعيدي استخدام `code` قديم. عالجي Client ID/Secret وRedirect URI وحالة Production، ثم أعيدي التفويض المحلي مرة واحدة.

## ضوابط مهمة

- لا تُشغِّل هذه الطريقة إنتاجًا أو رفعًا تجريبيًا أو نشرًا تلقائيًا.
- لا تشارك Google Client JSON أو الروابط التي تتضمن `code`.
- فحص الاتصال لا يؤكد صلاحية حصص رفع YouTube ولا سياسة ظهور الفيديو؛ هذه تحتاج اختبار نشر فعلي عند وجود حلقة **معتمدة** مسبقًا.
- راجعي `README.md`: نسخ Internet Archive المستخدمة للمراجعة قد تكون عامة قبل موافقة YouTube؛ سياسة المراجعة ليست خصوصية تلقائية.
