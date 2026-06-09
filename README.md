# دليل تشغيل تطبيق مراجعة الصور

## المحتويات
- `app.py` — التطبيق الرئيسي
- `requirements.txt` — التبعيات
- `.streamlit/secrets.toml.template` — نموذج للـ secrets (انسخيه باسم `secrets.toml`)

---

## الخطوة ١ — إعداد Google Cloud (مرة وحدة فقط، ~١٠ دقايق)

### ١.١ إنشاء مشروع Google Cloud (أو استخدام موجود)
1. روحي على https://console.cloud.google.com
2. أعلى الصفحة، اضغطي على dropdown المشروع → **New Project**
3. سمّيه مثلاً: `moarjam-review` → **Create**
4. تأكدي إن المشروع مختار في dropdown أعلى الصفحة

### ١.٢ تفعيل APIs المطلوبة
1. **APIs & Services → Library**
2. ابحثي عن `Google Sheets API` → افتحيها → **Enable**
3. ارجعي للـ Library → ابحثي عن `Google Drive API` → **Enable**

### ١.٣ إنشاء Service Account
1. **APIs & Services → Credentials**
2. **+ CREATE CREDENTIALS → Service Account**
3. الاسم: `review-app` (أي اسم) → **Create and Continue**
4. **Role** تجاوزيه (Continue) → **Done**
5. في صفحة Credentials، اضغطي على الـ service account اللي أنشأتيه
6. تبويب **KEYS → ADD KEY → Create new key → JSON → Create**
7. سيتم تنزيل ملف JSON تلقائياً — **احفظيه في مكان آمن** (هذا سرّك)

### ١.٤ مشاركة الـ Google Sheet مع الـ Service Account
1. افتحي ملف JSON اللي نزّلتيه → نسخ قيمة `client_email`
   (تشبه: `review-app@moarjam-review.iam.gserviceaccount.com`)
2. افتحي Google Sheet → زر **Share** (شارك) أعلى يمين
3. الصقي الإيميل → اختاري **Editor** → ✅ Notify people: **uncheck** → **Share**

---

## الخطوة ٢ — تجهيز الملفات محلياً

```bash
# انسخي الملفات على جهازك
mkdir review_app && cd review_app

# انسخي app.py + requirements.txt + .streamlit/secrets.toml.template
# (الملفات اللي عطيتك إياها)

# أنشئي مجلد .streamlit (لو ما هو موجود)
mkdir -p .streamlit
```

## الخطوة ٣ — تجهيز `secrets.toml`

```bash
cp .streamlit/secrets.toml.template .streamlit/secrets.toml
```

ثم افتحي `.streamlit/secrets.toml` بمحرر نصوص وعدّلي:

### `[sheet]`
- **`sheet_id`**: ID الشيت من الرابط:
  - الرابط: `https://docs.google.com/spreadsheets/d/1AbC...XYZ/edit`
  - الـ ID هو: `1AbC...XYZ`
- **`worksheet_name`**: اسم التبويب داخل الشيت (default: `enriched`)

### `[gcp_service_account]`
افتحي ملف الـ JSON اللي نزّلتيه من Google Cloud، وانسخي **كل حقل** في مكانه:

```toml
type            = "service_account"
project_id      = "moarjam-review"
private_key_id  = "abc123..."
private_key     = """-----BEGIN PRIVATE KEY-----
...المحتوى كامل من JSON بدون اقتباس مفرد...
-----END PRIVATE KEY-----
"""
client_email    = "review-app@moarjam-review.iam.gserviceaccount.com"
client_id       = "1234567890..."
# باقي الحقول كما في الـ template
```

> ⚠️ **مهم في `private_key`:**
> - استخدمي ثلاث علامات اقتباس `"""..."""`
> - احتفظي بـ `\n` كأسطر جديدة فعلية (مو سترينج literal)
> - لا تحذفي `-----BEGIN/END PRIVATE KEY-----`

---

## الخطوة ٤ — تثبيت + تشغيل

```bash
# في مجلد review_app
python -m venv .venv
source .venv/bin/activate          # على ويندوز: .venv\Scripts\activate

pip install -r requirements.txt

streamlit run app.py
```

سيُفتح المتصفح تلقائياً على `http://localhost:8501`.

---

## كيف يربط التطبيق بـ Google Sheet؟

عند بدء التشغيل:
1. التطبيق يقرأ `.streamlit/secrets.toml`
2. يفتح Sheet المحدد بـ `sheet_id`
3. يفحص الـ headers ويضيف الـ ١٢ عمود المراجعة إذا غير موجودة:
   - `review_status`, `review_decision`, `reviewer_name`, `reviewed_at`,
     `rejection_reason`, `reviewer_visual_note`, `needs_regeneration`,
     `regeneration_request_status`, `regenerated_prompt`, `regeneration_note`,
     `approved_image_url`, `previous_image_url`
4. يعرض فقط الصفوف اللي عندها `image_url` غير فاضي

عند الضغط على زر **اعتماد** أو **رفض**:
1. يطابق الصف بـ `image_uid` (الأساس)، ثم `image_filename`، ثم `sheet_row_number`
2. يحدّث **خلايا محددة فقط** في الصف الصحيح (batch update)
3. يمسح الـ cache → التحديث يبان فوراً

---

## استكشاف الأخطاء

| الخطأ | الحل |
|---|---|
| `KeyError: 'gcp_service_account'` | `.streamlit/secrets.toml` غير موجود أو فيه خطأ تنسيق |
| `APIError 403 PERMISSION_DENIED` | الشيت غير مُشارَك مع `client_email` الـ service account |
| `APIError 404 NOT FOUND` | `sheet_id` غلط |
| الصور ما تظهر | الصور في Drive لازم تكون "Anyone with link can view" — الـ Colab يسوي هذا تلقائياً |
| القرارات ما تنحفظ | تأكدي إن الـ service account له صلاحية **Editor** على الشيت (مو Viewer) |

---

## النشر على Streamlit Cloud لاحقاً

1. ارفعي `app.py` + `requirements.txt` فقط على GitHub (مو `secrets.toml`)
2. على https://share.streamlit.io اربطي الـ repo
3. في **Settings → Secrets** الصقي محتوى `.streamlit/secrets.toml`
4. التطبيق يصير live على رابط `https://<اسم>.streamlit.app`

---

## بنية الأعمدة المستخدمة

### أعمدة المصدر (يكتبها Colab):
- `lemma.formRepresentations[0].form` — الكلمة بالتشكيل
- `nonDiacriticsLemma` — الكلمة نظيفة
- `senses.definition.textRepresentations[0].form` — المعنى العربي
- `english_term` / `senses.translations[0].form` — الترجمة
- `object_description` — الوصف البصري الإنجليزي
- `image_prompt` — البرومت المرسل لـ gpt-image-1
- `image_url` — رابط Drive للصورة ⭐
- `image_filename` / `image_uid` / `sheet_row_number` — مفاتيح الربط

### أعمدة المراجعة (يضيفها التطبيق):
- `review_status` — `pending` / `approved` / `regeneration_requested`
- `review_decision` — `approved` / `rejected`
- `reviewer_name` — اسم المراجع
- `reviewed_at` — وقت المراجعة (بتوقيت الرياض)
- `rejection_reason` — سبب الرفض
- `reviewer_visual_note` — تصوّر المراجع للصورة المطلوبة
- `needs_regeneration` — `yes` / `no`
- `regeneration_request_status` — `pending` (للـ Agent لاحقاً)
- `approved_image_url` — يُملأ بـ `image_url` عند الاعتماد
- `previous_image_url` — يُملأ لاحقاً عند إعادة التوليد
- `regenerated_prompt`, `regeneration_note` — يستخدمها الـ Agent لاحقاً
