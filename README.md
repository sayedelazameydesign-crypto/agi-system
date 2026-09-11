# agi-system — نواة AGI Kernel (المقطع الرأسي)

نواة عميل ذكاء اصطناعي مستقل مكتوبة بـ **Python 3.10+ صرفة** — **بدون أي اعتمادات خارجية** (لا `pip install`، لا قاعدة بيانات، لا خدمات).
تُنفّذ حلقة إدراكية كاملة واحدة:

```
Goal → Planner → Policy → Executor → Verifier → Evidence → DecisionRecord → EventLedger
```

> **الفلسفة: لا ادعاءات بدون أدلة.**
> الحالة تصل إلى `VERIFIED` فقط بعد أن يتحقّق الـ **Verifier** من العالم بشكل مستقل — فهو لا يثق أبدًا بما يقوله الـ **Executor**.

---

## التشغيل السريع

```bash
git clone https://github.com/sayedelazameydesign-crypto/agi-system.git
cd agi-system

python3 demo.py                      # عرض تجريبي كامل (6 سيناريوهات)
python3 -m unittest discover -s tests -t . -v   # 39 اختبار وحدة
python3 demo.py --sandbox ./sb --keep           # الاحتفاظ ببيئة الاختبار لفحصها
```

لا يحتاج المشروع إلى بناء أو تثبيت: `python3` هو كل المطلوب.

---

## الاستخدام المباشر

```python
from core import Goal, Kernel

kernel = Kernel("./sandbox", "./ledger/events.jsonl")
record = kernel.run(Goal("create_file", {"path": "reports/a.txt", "content": "hello"}))

print(record.status.value)                 # VERIFIED | DENIED | FAILED | INCONCLUSIVE
print(record.evidence[0].checks)           # {'claimed_ok': True, 'file_exists': True, ...}
print(kernel.replay().chain_valid)         # True — سجل الأحداث سليم
```

مثال على الرفض (محاولة الخروج من الصندوق الرملي):

```python
kernel.run(Goal("create_file", {"path": "../../OUTSIDE.txt", "content": "x"}))
# → status = DENIED، ولا يُنفَّذ أي شيء، ولا يُكتب أي ملف خارج الصندوق
```

---

## الهيكل

```
core/
├── __init__.py        ← تصدير الواجهة العامة + __version__
├── models.py          ← كائنات البيانات (Goal, Action, Plan, Observation, Evidence,
│                        PolicyDecision, DecisionRecord, ExecStatus)
├── planner.py         ← واجهة Planner + SequentialPlanner (مع خطط بديلة)
├── policy.py          ← PolicyEngine (allowlist + احتواء sandbox + نموذج مخاطر)
├── executor.py        ← Executor (5 قدرات filesystem.*)
├── verifier.py        ← Verifier (فحوصات مستقلة لكل قدرة)
├── event_ledger.py    ← سجل JSONL (append-only + سلسلة هاش + قفل + replay)
└── kernel.py          ← المحور: الحلقة الكاملة + state management + resume + توازٍ
demo.py                ← 6 سيناريوهات توضيحية (سعيد، اختراق مسار، منفذ كاذب، ...)
tests/                 ← 39 اختبار وحدة، محمولة بالكامل (tempfile — تعمل على Windows)
```

---

## المكوّنات

| المكوّن | المسؤولية | ما **لا** يفعله |
|---|---|---|
| `models.py` | كائنات بيانات نقية + تسلسل JSON | لا يقوم بأي I/O |
| `planner.py` | تحويل `Goal` إلى `Plan`(ات) مرتبة | لا ينفّذ ولا يتحقق من صلاحيات |
| `policy.py` | allowlist + احتواء المسار + تقدير المخاطر | لا يلمس الملفات |
| `executor.py` | تنفيذ القدرات المسموحة فقط | لا يتحقق من الصلاحيات (مسؤولية Policy) |
| `verifier.py` | إعادة قراءة القرص وإنتاج أدلة | لا يثق بـ `Observation` أبدًا |
| `event_ledger.py` | سجل أحداث غير قابل للتعديل | لا يخزّن حالة مخفية |
| `kernel.py` | التنسيق، الحالة، الاستئناف، التوازي | لا يسمح باستثناء يخرج للخارج |

### حالات `ExecStatus`

```
PROPOSED → AUTHORIZED → EXECUTING → VERIFIED      (الدليل كامل ✓)
                    └──→ DENIED                   (السياسة رفضت)
                    └──→ FAILED                   (خطأ تخطيط/تنفيذ/تحقق)
                    └──→ INCONCLUSIVE             (ادّعى النجاح لكن الدليل ناقص)
```

---

## الأمان

* **احتواء الصندوق الرملي (sandbox):** كل مسار يُمرَّر عبر `Path.resolve()` ثم `relative_to(sandbox_root)`؛
  هذا يمنع `../`، والمسارات المطلقة الخارجية، وروابط `symlink` الهاربة — ويُعاد التحقق منه في الـ Executor كطبقة دفاع ثانية.
* **Fail-closed:** أي قدرة غير مدرجة في الـ allowlist تُرفض، والـ allowlist الفارغة ترفض كل شيء.
* **سجل محصّن:** سلسلة هاش (`prev` + `hash` = SHA-256) تكشف أي تعديل بأثر رجعي، والسجل يقع **خارج** الصندوق الرملي فلا يمكن للنواة نفسها تعديله.
* **لا استثناءات هاربة:** كل فشل يتحوّل إلى حالة نهائية + حدث في السجل.

---

## ما أُضيف مقارنةً بتحليل v0.1

التحليل الأصلي أورد 7 نقاط تحسين؛ **كلها مُعالجة في v0.2**:

| # | نقطة التحسين في v0.1 | الحالة في v0.2 |
|---|---|---|
| 1 | حاجز واحد (`filesystem.write` فقط) | 5 قدرات: `read` / `write` / `mkdir` / `list` / `delete` (+ أهداف: `create_file`, `read_file`, `append_file`, `delete_file`, `list_dir`, `ensure_dir`) |
| 2 | لا إدارة أخطاء محكمة | `Kernel.run()` لا يرمي استثناءً أبدًا: كل فشل → `FAILED` / `DENIED` / `INCONCLUSIVE` + حدث `KERNEL_ERROR` |
| 3 | لا خطط بديلة | `SequentialPlanner` يُنتج حتى 3 خطط مرتبة بالتكلفة، والنواة تختار أفضل خطة **مسموحة** |
| 4 | `predicted_risk=0.0` ثابت | نموذج مخاطر حقيقي (القدرة + حجم البيانات + عمق المسار + اتساع الحذف) مع نطاقات `low/medium/high` وحد أقصى قابل للضبط |
| 5 | الاختبارات خاصة بـ Linux | 39 اختبارًا تستخدم `tempfile`/`pathlib` — محمولة على Linux و macOS و Windows |
| 6 | لا دعم للتزامن | `EventLedger` آمن بين الخيوط (`RLock` + `flock` على POSIX + ملف `.lock` محمول) و`Kernel.run_many()` ينفّذ الأهداف بالتوازي |
| 7 | لا إدارة حالة | السجل هو مصدر الحقيقة: `replay()` يعيد بناء كل التشغيلات، و`resume(run_id)` يُكمل تشغيلًا انقطع (حتى من عملية جديدة تمامًا) |

---

---

## المرحلة 0: التثبيت (Hardening) — الحالة: مكتملة جزئيًا ✓

الهدف: **نظام أكثر قوة وقابلية للتشغيل** — بدون ذكاء جديد، فقط جاهزية تشغيل حقيقية.

| البند | الملف | الحالة |
|---|---|---|
| تصنيف أخطاء موحّد بأكواد ثابتة | `core/errors.py` | ✓ |
| إعدادات من ملف JSON/TOML + متغيرات بيئة `AGI_*` | `core/config.py` | ✓ |
| ساعة قابلة للحقن (اختبارات حتمية) | `core/clock.py` | ✓ |
| تسجيل structured (نص/JSON) | `core/logging_setup.py` | ✓ |
| مقاييس (counters/gauges/durations) + `metrics.json` | `core/metrics.py` | ✓ |
| كتابة ذرية للملفات (temp + `os.replace` + `fsync`) | `core/executor.py` | ✓ |
| مهلة لكل إجراء + مهلة لكل تشغيل | `core/kernel.py` | ✓ |
| إعادة محاولات محدودة Budget (معطّلة افتراضيًا) | `core/kernel.py` | ✓ |
| حصة ملفات (quota) داخل الصندوق الرملي | `core/executor.py` | ✓ |
| إيقاف تعاوني (`request_stop`) وحالة `ABORTED` | `core/kernel.py` | ✓ |
| `fsck` / `quarantine` / `snapshot` / `rotate` للسجل | `core/event_ledger.py` | ✓ |
| إصدار مخطط الأحداث (`schema_version`) | `core/config.py` | ✓ |
| فحص صحة شامل `Kernel.health()` | `core/kernel.py` | ✓ |
| واجهة سطر أوامر (`agi-kernel`) | `core/cli.py` | ⏳ قيد التنفيذ |
| حزمة قابلة للتثبيت + CI | `pyproject.toml`, `.github/` | ⏳ قيد التنفيذ |

> ملاحظة معمارية: المنفذ **لا** يبتلع `KeyboardInterrupt`/`SystemExit` — فهي تعود للعملية،
> ويبقى السجل هو مصدر الحقيقة لإكمال التشغيل عبر `resume()`.

## خريطة الطريق المقترحة

1. قدرات جديدة: `process.execute` (مقيّد)، `network.fetch` (للقراءة فقط)، `vector.store`.
2. مخطط مدعوم بنموذج لغوي (LLM) خلف واجهة `Planner` نفسها — دون تعديل النواة.
3. تحقّق دلالي (هل المحتوى **صحيح معنويًا**؟) بجانب التحقق البنيوي الحالي.
4. تحديد معدّل (rate limiting) وميزانية تكلفة لكل تشغيل.
5. تصدير السجل إلى صيغ قابلة للتدقيق الخارجي (مخرجات只读 + توقيع).

---

## الترخيص

لم يُحدَّد بعد — أضف ملف `LICENSE` إن رغبت.
