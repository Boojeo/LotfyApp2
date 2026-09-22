"""Bilingual presentation layer (English / Arabic).

Two rules hold this together:

1. **Nothing stored is translated.**  ``BUY``, ``MANAGING``, ``TP1`` and every
   other enum value stays English in SQLite and on the wire.  Switching
   language re-renders text; it never rewrites a record.  That is what makes
   ``/lang`` safe to use mid-trade.

2. **Every interpolated value is bidi-isolated in Arabic.**  A price dropped
   raw into right-to-left text renders scrambled -- "SL 3398.10" can come out
   as "3398.10 SL" or worse, with the digits reordered.  Wrapping each value in
   FSI/PDI tells the renderer to lay that run out on its own.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

LANGUAGES = ("en", "ar")
RTL_LANGUAGES = frozenset({"ar"})

# First Strong Isolate / Pop Directional Isolate.
_FSI = "⁨"
_PDI = "⁩"


def isolate(value: Any) -> str:
    """Wrap a value so bidi layout treats it as its own run."""
    return f"{_FSI}{value}{_PDI}"


class Translator:
    """Renders message keys in the active language.

    Missing keys fall back to English rather than raising -- a missing
    translation must never stop a stop-loss alert from reaching you.
    """

    def __init__(self, language: str = "en"):
        self.language = language if language in LANGUAGES else "en"

    @property
    def is_rtl(self) -> bool:
        return self.language in RTL_LANGUAGES

    @property
    def direction(self) -> str:
        return "rtl" if self.is_rtl else "ltr"

    def with_language(self, language: str) -> "Translator":
        return Translator(language)

    def __call__(self, key: str, **kwargs: Any) -> str:
        entry = CATALOG.get(key)
        if entry is None:
            # Surfacing the key beats silently dropping the message.
            return f"[{key}]" + (f" {kwargs}" if kwargs else "")
        template = entry.get(self.language) or entry["en"]
        if self.is_rtl and kwargs:
            kwargs = {
                name: value if _FSI in str(value) else isolate(value)
                for name, value in kwargs.items()
            }
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return entry["en"].format(**kwargs)

    # ------------------------------------------------------------------ terms

    def term(self, group: str, value: str) -> str:
        """Translate a stored enum value for display only."""
        return self(f"term.{group}.{str(value).lower()}") if (
            f"term.{group}.{str(value).lower()}" in CATALOG
        ) else str(value)

    def direction_name(self, value: Any) -> str:
        return self.term("direction", getattr(value, "value", value))

    def bias_name(self, value: Any) -> str:
        return self.term("bias", getattr(value, "value", value))

    def strength_name(self, value: Any) -> str:
        return self.term("strength", getattr(value, "value", value))

    def join(self, parts: Iterable[str]) -> str:
        separator = "، " if self.is_rtl else ", "
        return separator.join(parts)

    @property
    def semicolon(self) -> str:
        return "؛ " if self.is_rtl else "; "


# ---------------------------------------------------------------------------
# Catalogue.  Keys are dotted and grouped by the surface they appear on.
# ---------------------------------------------------------------------------

CATALOG: Dict[str, Dict[str, str]] = {
    # ---------------------------------------------------------------- terms
    "term.direction.buy": {"en": "BUY", "ar": "شراء"},
    "term.direction.sell": {"en": "SELL", "ar": "بيع"},
    "term.bias.bullish": {"en": "BULLISH", "ar": "صاعد"},
    "term.bias.bearish": {"en": "BEARISH", "ar": "هابط"},
    "term.bias.neutral": {"en": "NEUTRAL", "ar": "محايد"},
    "term.strength.strong": {"en": "STRONG", "ar": "قوي"},
    "term.strength.moderate": {"en": "MODERATE", "ar": "متوسط"},
    "term.strength.weak": {"en": "WEAK", "ar": "ضعيف"},

    # ---------------------------------------------------------------- startup
    "startup.online": {
        "en": "Trade manager online ({environment}{dry_run}) on account {account}.",
        "ar": "مدير الصفقات يعمل الآن ({environment}{dry_run}) على الحساب {account}.",
    },
    "startup.dry_run": {"en": ", DRY RUN", "ar": "، وضع تجريبي"},
    "startup.watching": {"en": "Watching: {epics}", "ar": "المتابعة: {epics}"},
    "startup.exit_model": {"en": "Exit model: {model}", "ar": "نموذج الخروج: {model}"},
    "startup.hedging_off": {
        "en": (
            "Exit model is THREE_DEALS but this account has hedging OFF, so "
            "Capital.com will merge your three deals into one position and the "
            "legs cannot be closed separately.\n"
            "Turn hedging on, or switch management.exit_model to partial_close."
        ),
        "ar": (
            "نموذج الخروج هو ثلاث صفقات، لكن التحوط معطّل في هذا الحساب، لذلك "
            "ستدمج المنصة صفقاتك الثلاث في مركز واحد ولن يمكن إغلاق كل صفقة على حدة.\n"
            "فعّل التحوط، أو غيّر نموذج الخروج إلى partial_close."
        ),
    },
    "startup.partials_unavailable": {
        "en": (
            "Partial closes are NOT available on this account:\n{detail}\n"
            "TP1/TP2 partials will be skipped; break-even and trailing still apply."
        ),
        "ar": (
            "الإغلاق الجزئي غير متاح في هذا الحساب:\n{detail}\n"
            "سيتم تخطي الإغلاق الجزئي عند الهدف الأول والثاني، مع بقاء نقطة "
            "التعادل ووقف الخسارة المتحرك."
        ),
    },

    # ---------------------------------------------------------------- adoption
    "adoption.detected": {
        "en": "New position detected: {epic} {direction} {size} @ {price}",
        "ar": "تم رصد مركز جديد: {epic} {direction} {size} بسعر {price}",
    },
    "adoption.plan": {
        "en": "Plan {plan_id} -- bias {bias}",
        "ar": "الخطة {plan_id} — الاتجاه المتوقع {bias}",
    },
    "adoption.against_bias": {
        "en": "  (NOTE: your entry is against the plan's bias)",
        "ar": "  (تنبيه: دخولك معاكس لاتجاه الخطة)",
    },
    "adoption.levels": {
        "en": "SL {sl}   TP1 {tp1}   TP2 {tp2}   TP3 {tp3}",
        "ar": "وقف الخسارة {sl}   الهدف الأول {tp1}   الهدف الثاني {tp2}   الهدف الثالث {tp3}",
    },
    "adoption.plan_leg": {
        "en": "leg {index} of {total} -- this deal closes in full at {target}",
        "ar": "الصفقة {index} من {total} — تُغلق بالكامل عند {target}",
    },
    "adoption.plan_ladder": {
        "en": "Ladder: {steps}",
        "ar": "سلّم الإغلاق: {steps}",
    },
    "adoption.plan_rules": {
        "en": "break-even at {breakeven}; trail after {trail}",
        "ar": "نقطة التعادل عند {breakeven}؛ الوقف المتحرك بعد {trail}",
    },
    "adoption.waiting_legs": {
        "en": (
            "WAITING on {count} more deal(s) to complete the basket. Open them "
            "within {minutes} minutes, or this deal closes at {target} on its own."
        ),
        "ar": (
            "في انتظار {count} صفقة أخرى لاكتمال السلة. افتحها خلال {minutes} "
            "دقيقة، وإلا ستُغلق هذه الصفقة عند {target} بمفردها."
        ),
    },
    "adoption.confirm_hint": {
        "en": "/confirm {id} to manage it, /decline {id} to leave it alone.",
        "ar": "أرسل ‎/confirm {id}‎ لإدارتها، أو ‎/decline {id}‎ لتركها دون إدارة.",
    },
    "adoption.no_plan": {
        "en": (
            "New {direction} {size} {epic} @ {price} detected, but no plan could "
            "be built ({error}). It is NOT being managed."
        ),
        "ar": (
            "تم رصد {direction} {size} {epic} بسعر {price}، لكن تعذّر بناء خطة "
            "({error}). لن تتم إدارة هذا المركز."
        ),
    },
    "adoption.late_leg": {
        "en": (
            "{epic}: leg {index} joined the confirmed group ({size} @ {price}), "
            "exits at {target}."
        ),
        "ar": (
            "{epic}: انضمت الصفقة {index} إلى السلة المؤكدة ({size} بسعر {price})، "
            "وتخرج عند {target}."
        ),
    },
    "adoption.timeout": {
        "en": (
            "{epic} ({id}) was not confirmed within {minutes} minutes -- leaving "
            "it unmanaged. Use /manage to take it over later."
        ),
        "ar": (
            "لم يتم تأكيد {epic} ({id}) خلال {minutes} دقيقة — سيُترك دون إدارة. "
            "استخدم ‎/manage‎ لاحقًا لإدارته."
        ),
    },
    "adoption.managing": {
        "en": "Managing {epic} {direction} {size} @ {price} ({label}).",
        "ar": "تتم الآن إدارة {epic} {direction} {size} بسعر {price} ({label}).",
    },
    "adoption.label_leg": {
        "en": "leg {index} -> {target}",
        "ar": "الصفقة {index} ← {target}",
    },
    "adoption.label_position": {"en": "position", "ar": "المركز"},
    "adoption.declined": {
        "en": "Leaving {epic} ({id}) unmanaged.",
        "ar": "سيُترك {epic} ({id}) دون إدارة.",
    },
    "adoption.not_found": {
        "en": "No trade matching {id}.",
        "ar": "لا توجد صفقة مطابقة لـ {id}.",
    },
    "adoption.already": {
        "en": "{epic} ({id}) is already {status}.",
        "ar": "{epic} ({id}) بالفعل في حالة {status}.",
    },

    # ---------------------------------------------------------------- actions
    "action.header": {
        "en": "{epic} ({id}) @ {price} | {r}R",
        "ar": "{epic} ({id}) بسعر {price} | {r}R",
    },
    "action.partial_close": {
        "en": "closed {size} at {stage}: {reason}",
        "ar": "أُغلق {size} عند {stage}: {reason}",
    },
    "action.close_all": {
        "en": "closed the remaining {size}: {reason}",
        "ar": "أُغلق المتبقي {size}: {reason}",
    },
    "action.set_stop": {
        "en": "stop moved to {level}: {reason}",
        "ar": "نُقل وقف الخسارة إلى {level}: {reason}",
    },
    "action.set_target": {
        "en": "target moved to {level}: {reason}",
        "ar": "نُقل الهدف إلى {level}: {reason}",
    },
    "action.failed": {
        "en": (
            "FAILED on {epic} ({id}): {action}\n{error}\n"
            "The broker rejected this -- check the position manually."
        ),
        "ar": (
            "فشل التنفيذ على {epic} ({id}): {action}\n{error}\n"
            "رفضت المنصة هذه العملية — تحقق من المركز يدويًا."
        ),
    },
    "action.overclosed": {
        "en": (
            "WARNING {epic} ({id}): asked to close {size} but the whole position "
            "is gone (expected {expected} to remain). Partial closes are being "
            "disabled for safety -- check the account."
        ),
        "ar": (
            "تحذير {epic} ({id}): طُلب إغلاق {size} لكن المركز أُغلق بالكامل "
            "(كان يُفترض بقاء {expected}). سيتم تعطيل الإغلاق الجزئي للسلامة — "
            "تحقق من الحساب."
        ),
    },
    "action.size_mismatch": {
        "en": (
            "WARNING {epic} ({id}): after closing {size} the broker reports "
            "{actual} remaining, expected {expected}."
        ),
        "ar": (
            "تحذير {epic} ({id}): بعد إغلاق {size} تُظهر المنصة {actual} متبقيًا، "
            "بينما المتوقع {expected}."
        ),
    },
    "action.closed_at_broker": {
        "en": "{epic} ({id}) is closed at the broker. Ladder reached: {stage}.",
        "ar": "{epic} ({id}) أُغلق لدى المنصة. أعلى مرحلة تم بلوغها: {stage}.",
    },
    "action.stage_none": {"en": "none", "ar": "لا شيء"},

    # ---------------------------------------------------------------- reasons
    "reason.stage_reached": {
        "en": "{stage} {level} reached at {price}",
        "ar": "تم بلوغ {stage} عند {level} بسعر {price}",
    },
    "reason.leg_target": {
        "en": "leg {index} target {stage} {level} reached at {price}",
        "ar": "الصفقة {index}: تم بلوغ هدفها {stage} عند {level} بسعر {price}",
    },
    "reason.breakeven": {
        "en": "{stage} reached -- stop to entry {level}",
        "ar": "تم بلوغ {stage} — نقل الوقف إلى سعر الدخول {level}",
    },
    "reason.indivisible": {
        "en": "{stage} hit and the remainder would be below the minimum deal size",
        "ar": "تم بلوغ {stage} وكان المتبقي سيقل عن الحد الأدنى لحجم الصفقة",
    },
    "reason.extend": {
        "en": "strong trend into {stage} {level} -- extending to {new} ({count}/{limit})",
        "ar": "اتجاه قوي نحو {stage} عند {level} — تمديد الهدف إلى {new} ({count}/{limit})",
    },
    "reason.trail": {
        "en": "{strength} trend, k={k}, best {best}, ATR {atr} -> stop {level}",
        "ar": "اتجاه {strength}، المعامل {k}، أفضل سعر {best}، المدى {atr} ← الوقف {level}",
    },
    "reason.initial_stop": {
        "en": "initial protective stop from plan {plan_id}",
        "ar": "وقف الحماية الابتدائي وفق الخطة {plan_id}",
    },
    "reason.initial_target": {
        "en": "final target from plan {plan_id}",
        "ar": "الهدف النهائي وفق الخطة {plan_id}",
    },
    "reason.manual_close": {
        "en": "manual close requested",
        "ar": "طلب إغلاق يدوي",
    },
    "reason.manual_breakeven": {
        "en": "manual break-even requested",
        "ar": "طلب نقل الوقف إلى نقطة التعادل يدويًا",
    },

    # ---------------------------------------------------------------- connection
    "degraded.lost": {
        "en": (
            "Lost contact with {broker}: {reason}\n"
            "Holding all state; no decisions will be taken until the connection "
            "recovers. Positions already carry their broker-side stop."
        ),
        "ar": (
            "انقطع الاتصال بـ {broker}: {reason}\n"
            "سيتم تجميد الحالة دون اتخاذ أي قرارات حتى يعود الاتصال. المراكز "
            "المفتوحة محمية بوقف الخسارة المسجّل لدى المنصة."
        ),
    },
    "degraded.recovered": {
        "en": "Connection to the broker recovered; management resumed.",
        "ar": "عاد الاتصال بالمنصة؛ استُؤنفت الإدارة.",
    },

    # ---------------------------------------------------------------- status
    "status.paused": {
        "en": "** management is PAUSED **",
        "ar": "** الإدارة متوقفة مؤقتًا **",
    },
    "status.degraded": {
        "en": "** broker connection degraded **",
        "ar": "** الاتصال بالمنصة متعثر **",
    },
    "status.awaiting": {
        "en": "[awaiting confirmation] {epic} {direction} {size} @ {price} -> /confirm {id}",
        "ar": "[بانتظار التأكيد] {epic} {direction} {size} بسعر {price} ← ‎/confirm {id}‎",
    },
    "status.line": {
        "en": "{epic} {direction} {remaining}/{initial} @ {entry} | now {marker}",
        "ar": "{epic} {direction} {remaining}/{initial} بسعر {entry} | الآن {marker}",
    },
    "status.levels": {
        "en": "   SL {sl} TP1 {tp1} TP2 {tp2} TP3 {tp3} [{flags}] id {id}",
        "ar": "   الوقف {sl} الهدف١ {tp1} الهدف٢ {tp2} الهدف٣ {tp3} [{flags}] المعرف {id}",
    },
    "status.price_unavailable": {"en": "price unavailable", "ar": "السعر غير متاح"},
    "status.empty": {
        "en": "No positions are being managed.",
        "ar": "لا توجد مراكز تحت الإدارة.",
    },
    "status.paused_now": {
        "en": "Trade management PAUSED -- no stops or targets will be modified.",
        "ar": "تم إيقاف إدارة الصفقات مؤقتًا — لن يتم تعديل أي وقف أو هدف.",
    },
    "status.resumed_now": {
        "en": "Trade management resumed.",
        "ar": "استُؤنفت إدارة الصفقات.",
    },

    # ---------------------------------------------------------------- commands
    "command.unknown": {
        "en": "Unknown command /{command}. Try /help.",
        "ar": "أمر غير معروف ‎/{command}‎. جرّب ‎/help‎.",
    },
    "command.failed": {
        "en": "/{command} failed: {error}",
        "ar": "فشل الأمر ‎/{command}‎: {error}",
    },
    "command.language_set": {
        "en": "Language set to English. Stored records are unchanged.",
        "ar": "تم ضبط اللغة على العربية. لم تتغير أي بيانات مخزّنة.",
    },
    "command.language_usage": {
        "en": "Usage: /lang en  or  /lang ar  (currently: {current})",
        "ar": "الاستخدام: ‎/lang en‎ أو ‎/lang ar‎ (اللغة الحالية: {current})",
    },
    "command.no_epic": {
        "en": "No epic given and the watchlist is empty.",
        "ar": "لم يتم تحديد رمز والقائمة فارغة.",
    },
    "command.plan_usage": {
        "en": "Usage: /plan <epic>",
        "ar": "الاستخدام: ‎/plan <رمز الأداة>‎",
    },
    "command.help": {
        "en": (
            "/status            open positions and ladder state\n"
            "/confirm <id>      start managing a detected position\n"
            "/decline <id>      leave a detected position alone\n"
            "/manage <id>       take over a position declined earlier\n"
            "/report [epic]     rebuild and send the full plan\n"
            "/plan [epic]       show the stored plan levels\n"
            "/close <id>        close the remaining size now\n"
            "/be <id>           move the stop to entry now\n"
            "/lang en|ar        switch language\n"
            "/pause /resume     stop or restart all order modifications"
        ),
        "ar": (
            "‎/status‎            المراكز المفتوحة ومراحل الإغلاق\n"
            "‎/confirm <id>‎      بدء إدارة مركز تم رصده\n"
            "‎/decline <id>‎      ترك المركز دون إدارة\n"
            "‎/manage <id>‎       إدارة مركز سبق رفضه\n"
            "‎/report [epic]‎     إعادة بناء الخطة وإرسالها\n"
            "‎/plan [epic]‎       عرض مستويات الخطة المحفوظة\n"
            "‎/close <id>‎        إغلاق الكمية المتبقية الآن\n"
            "‎/be <id>‎           نقل الوقف إلى سعر الدخول الآن\n"
            "‎/lang en|ar‎        تغيير اللغة\n"
            "‎/pause /resume‎     إيقاف أو استئناف تعديل الأوامر"
        ),
    },

    # ---------------------------------------------------------------- chart
    "chart.title": {
        "en": "{epic} -- {bias} ({confidence}/100)",
        "ar": "{epic} — {bias} {confidence}/100",
    },
    "chart.entry": {"en": "entry", "ar": "الدخول"},
    "chart.atr": {"en": "ATR {value}", "ar": "المدى {value}"},
    "chart.adx": {"en": "ADX {value} ({strength})", "ar": "ADX {value} {strength}"},
    "chart.rsi": {"en": "RSI {value}", "ar": "RSI {value}"},
    "chart.risk": {"en": "risk {value}", "ar": "المخاطرة {value}"},
    "chart.unavailable": {
        "en": "Chart could not be drawn ({error}); the text plan above still applies.",
        "ar": "تعذّر رسم الشارت ({error})؛ الخطة النصية أعلاه سارية.",
    },

    # ---------------------------------------------------------------- report
    "report.title": {
        "en": "{epic} -- daily plan {timestamp}",
        "ar": "{epic} — الخطة اليومية {timestamp}",
    },
    "report.bias": {
        "en": "Direction bias: {bias}  (confidence {confidence}/100)",
        "ar": "الاتجاه المتوقع: {bias}  (درجة الثقة {confidence}/100)",
    },
    "report.advisory": {
        "en": (
            "Bias is NEUTRAL. Levels below are reference only -- the bot will "
            "still manage a position you open, but nothing here argues for "
            "taking one."
        ),
        "ar": (
            "الاتجاه محايد. المستويات أدناه للاسترشاد فقط — سيدير البوت أي مركز "
            "تفتحه، لكن لا شيء هنا يدعو لفتح صفقة."
        ),
    },
    "report.reference": {
        "en": "Reference price {price} | ATR {atr} | risk to stop {risk}",
        "ar": "السعر المرجعي {price} | متوسط المدى الحقيقي {atr} | المخاطرة حتى الوقف {risk}",
    },
    "report.levels_heading": {"en": "Levels", "ar": "المستويات"},
    "report.col_level": {"en": "Level", "ar": "المستوى"},
    "report.col_price": {"en": "Price", "ar": "السعر"},
    "report.col_distance": {"en": "Distance", "ar": "المسافة"},
    "report.col_r": {"en": "R multiple", "ar": "مضاعف المخاطرة"},
    "report.technical_heading": {"en": "Technical", "ar": "التحليل الفني"},
    "report.technical_summary": {
        "en": "Score {score} ({bias}), ADX {adx} ({strength}), RSI {rsi}",
        "ar": "النتيجة {score} ({bias})، مؤشر ADX {adx} ({strength})، مؤشر RSI {rsi}",
    },
    "report.col_factor": {"en": "Factor", "ar": "العامل"},
    "report.col_value": {"en": "Value", "ar": "القيمة"},
    "report.col_weight": {"en": "Weight", "ar": "الوزن"},
    "report.col_detail": {"en": "Detail", "ar": "التفاصيل"},
    "report.fundamental_heading": {"en": "Fundamental / news", "ar": "التحليل الأساسي والأخبار"},
    "report.fundamental_summary": {
        "en": "{bias} (confidence {confidence}, source {source})",
        "ar": "{bias} (درجة الثقة {confidence}، المصدر {source})",
    },
    "report.drivers": {"en": "Drivers", "ar": "الدوافع"},
    "report.risks": {"en": "Risks", "ar": "المخاطر"},
    "report.catalysts": {"en": "Catalysts", "ar": "المحفزات"},
    "report.liquidity_heading": {"en": "Liquidity zones", "ar": "مناطق السيولة"},
    "report.headlines_heading": {"en": "Headlines", "ar": "العناوين"},
    "report.plan_id": {"en": "plan id: {id}", "ar": "معرّف الخطة: {id}"},
    "report.tech_label": {
        "en": "tech {score} / {strength} | news {bias} ({source})",
        "ar": "فني {score} / {strength} | أخبار {bias} ({source})",
    },
    "report.advisory_tag": {"en": "  [advisory only]", "ar": "  [للاسترشاد فقط]"},
    "report.failed": {
        "en": "{epic}: report failed -- {error}",
        "ar": "{epic}: تعذّر إنشاء التقرير — {error}",
    },
    "report.no_plan": {
        "en": "No stored plan for {epic}. Try /report {epic}.",
        "ar": "لا توجد خطة محفوظة لـ {epic}. جرّب ‎/report {epic}‎.",
    },
    "report.update_heading": {"en": "{epic} plan update:", "ar": "تحديث خطة {epic}:"},
    "report.update_bias": {
        "en": "bias {old} -> {new} (confidence {confidence})",
        "ar": "الاتجاه {old} ← {new} (درجة الثقة {confidence})",
    },
    "report.update_levels": {
        "en": "New levels: SL {sl} | TP1 {tp1} TP2 {tp2} TP3 {tp3}",
        "ar": "المستويات الجديدة: الوقف {sl} | الهدف١ {tp1} الهدف٢ {tp2} الهدف٣ {tp3}",
    },
}


def catalogue_report() -> Dict[str, Any]:
    """Coverage summary, used by the tests and by ``tmbot check``."""
    missing = sorted(key for key, entry in CATALOG.items() if not entry.get("ar"))
    return {"keys": len(CATALOG), "missing_ar": missing}
