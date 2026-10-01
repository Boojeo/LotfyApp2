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
        # Deal types stay plain words; arrows belong to the analysis only.
        return self.term("direction", getattr(value, "value", value))

    def bias_name(self, value: Any, *, arrow: bool = True) -> str:
        return self._arrowed("bias", getattr(value, "value", value), arrow)

    def _arrowed(self, group: str, value: Any, arrow: bool) -> str:
        """The translated word, led by an up/down arrow so it reads at a glance.

        Pass arrow=False where emoji cannot be drawn (chart images).
        """
        name = self.term(group, value)
        mark = ARROWS.get(str(value).lower()) if arrow else None
        return f"{mark} {name}" if mark else name

    def strength_name(self, value: Any) -> str:
        return self.term("strength", getattr(value, "value", value))

    def join(self, parts: Iterable[str]) -> str:
        separator = "، " if self.is_rtl else ", "
        return separator.join(parts)

    @property
    def semicolon(self) -> str:
        return "؛ " if self.is_rtl else "; "


# Analysis bias only: up for bullish, down for bearish. Neutral gets a flat
# dash, not a sideways arrow: an arrow there reads as a direction it is not.
ARROWS = {
    "bullish": "\u2b06\ufe0f",
    "bearish": "\u2b07\ufe0f",
    "neutral": "\u2796",
}


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
    "term.style.scalp": {"en": "Scalp", "ar": "سكالبينج"},
    "term.style.intraday": {"en": "Intraday", "ar": "تداول يومي"},
    "term.style.swing": {"en": "Swing", "ar": "سوينج"},

    # ---------------------------------------------------------------- trade style
    "style.why_fixed": {
        "en": "Timeframes are fixed in the settings.",
        "ar": "الأطر الزمنية ثابتة في الإعدادات.",
    },
    "style.why_only": {
        "en": "Only one style is allowed.",
        "ar": "نمط واحد فقط مسموح به.",
    },
    "style.why_trend": {
        "en": "Why: {slow} has a strong trend (ADX {adx}, needs {min}) in the same "
              "direction as {fast}, so targets get more room.",
        "ar": "السبب: يوجد اتجاه قوي على {slow} (ADX {adx}، المطلوب {min}) في نفس "
              "اتجاه {fast}، لذلك أُعطيت الأهداف مساحة أكبر.",
    },
    "style.why_no_trend": {
        "en": "Why: no strong trend on {slow} (ADX {adx}, needs {min}), so the "
              "targets stay quick.",
        "ar": "السبب: لا يوجد اتجاه قوي على {slow} (ADX {adx}، المطلوب {min})، "
              "لذلك تبقى الأهداف قريبة وسريعة.",
    },
    "style.why_disagree": {
        "en": "Why: {slow} does not point the same way as {fast}, so the targets "
              "stay quick.",
        "ar": "السبب: اتجاه {slow} لا يتفق مع اتجاه {fast}، لذلك تبقى الأهداف "
              "قريبة وسريعة.",
    },
    "style.why_no_data": {
        "en": "Why: not enough {slow} history to judge a longer trade.",
        "ar": "السبب: لا توجد بيانات كافية على {slow} للحكم على صفقة أطول.",
    },

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
            "the broker will merge your three deals into one position and the "
            "legs cannot be closed separately.\n"
            "Turn hedging on, or switch management.exit_model to partial_close."
        ),
        "ar": (
            "نموذج الخروج هو ثلاث صفقات، لكن التحوط معطّل في هذا الحساب، لذلك "
            "ستدمج المنصة صفقاتك الثلاث في مركز واحد ولن يمكن إغلاق كل صفقة على حدة.\n"
            "فعّل التحوط، أو غيّر نموذج الخروج إلى partial_close."
        ),
    },
    "startup.algo_trading_off": {
        "en": (
            "MetaTrader 5 has Algo Trading switched OFF, so the bot cannot move "
            "stops or close deals -- every attempt will be refused.\n"
            "Click the Algo Trading button in the MT5 toolbar until it turns green."
        ),
        "ar": (
            "زر التداول الآلي (Algo Trading) مغلق في ميتاتريدر 5، لذلك لا يستطيع "
            "البوت تحريك وقف الخسارة أو إغلاق الصفقات، وستُرفض كل محاولة.\n"
            "اضغط زر Algo Trading في شريط أدوات MT5 حتى يصبح أخضر."
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
    "reason.risk_cut": {
        "en": "{progress} of the way to TP1 -- risk cut to {left}, stop moved to {level}",
        "ar": "قطع السعر {progress} من الطريق إلى الهدف الأول — خُفّضت المخاطرة "
              "إلى {left} ونُقل الوقف إلى {level}",
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
    "reason.reversal_tighten": {
        "en": "reversal ({detail}) -- stop tightened to {level}",
        "ar": "انعكاس ({detail}) — تم تضييق الوقف إلى {level}",
    },
    "reason.reversal_close": {
        "en": "trend reversed against the position: {detail}",
        "ar": "انعكس الاتجاه ضد المركز: {detail}",
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

    # ---------------------------------------------------------------- reversal signals
    "signal.di_cross.buy": {
        "en": "-DI crossed +DI (+DI {plus} / -DI {minus})",
        "ar": "تقاطع -DI فوق +DI (+DI {plus} / -DI {minus})",
    },
    "signal.di_cross.sell": {
        "en": "+DI crossed -DI (+DI {plus} / -DI {minus})",
        "ar": "تقاطع +DI فوق -DI (+DI {plus} / -DI {minus})",
    },
    "signal.structure": {
        "en": "structure broke {level}",
        "ar": "كسر البنية السعرية عند {level}",
    },
    "signal.ema_cross": {
        "en": "EMA20 crossed EMA50",
        "ar": "تقاطع المتوسط 20 مع المتوسط 50",
    },
    "signal.momentum": {
        "en": "MACD flipped to {value}",
        "ar": "انقلب مؤشر MACD إلى {value}",
    },

    # ---------------------------------------------------------------- reversal alert
    "reversal.header": {
        "en": "REVERSAL on {epic} ({id})",
        "ar": "انعكاس في {epic} ({id})",
    },
    "reversal.evidence": {
        "en": "{agreeing}/{total} signals agree, ADX {adx} -- {detail}",
        "ar": "{agreeing} من {total} إشارات متوافقة، ADX {adx} — {detail}",
    },
    "reversal.position": {
        "en": "Your {direction} position is {r}R",
        "ar": "مركزك {direction} عند {r}R",
    },
    "reversal.acted": {
        "en": "Action taken: {action}",
        "ar": "الإجراء المتخذ: {action}",
    },
    "reversal.no_action": {
        "en": (
            "No action taken -- the bot never opens a position to recover. "
            "Decide yourself:"
        ),
        "ar": (
            "لم يُتخذ أي إجراء — البوت لا يفتح أي مركز للتعويض. القرار لك:"
        ),
    },
    "reversal.options": {
        "en": "/close {id} to exit now  |  /hold {id} to stop these alerts on this trade",
        "ar": "‎/close {id}‎ للخروج الآن  |  ‎/hold {id}‎ لإيقاف هذه التنبيهات لهذه الصفقة",
    },
    "reversal.muted": {
        "en": "Reversal alerts muted for {epic} ({id}). It is still managed normally.",
        "ar": "تم كتم تنبيهات الانعكاس لـ {epic} ({id}). تستمر إدارته بشكل طبيعي.",
    },
    "reversal.unmuted": {
        "en": "Reversal alerts back on for {epic} ({id}).",
        "ar": "أُعيد تفعيل تنبيهات الانعكاس لـ {epic} ({id}).",
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
    "now.heading": {
        "en": "LIVE {time} ({zone})",
        "ar": "مباشر {time} ({zone})",
    },
    "now.line": {
        "en": "{epic}  {bid} / {ask}  today {change}  {bias} {confidence}/100  {style}",
        "ar": "{epic}  {bid} / {ask}  اليوم {change}  {bias} {confidence}/100  {style}",
    },
    "now.analysis": {"en": "ANALYSIS NOW", "ar": "التحليل الآن"},
    "now.signals": {"en": "Signals ({timeframe}):", "ar": "الإشارات ({timeframe}):"},
    "now.readings": {
        "en": "ADX {adx} {strength} | RSI {rsi} {zone}",
        "ar": "ADX {adx} {strength} | RSI {rsi} {zone}",
    },
    "now.rsi_high": {"en": "(overbought)", "ar": "(تشبع شرائي)"},
    "now.rsi_low": {"en": "(oversold)", "ar": "(تشبع بيعي)"},
    "now.rsi_mid": {"en": "(neutral)", "ar": "(محايد)"},
    "now.nearest": {
        "en": "Nearest support {support} | resistance {resistance}",
        "ar": "أقرب دعم {support} | مقاومة {resistance}",
    },
    "now.footer": {
        "en": "Saved as the current plan. /report adds news and the chart.",
        "ar": "حُفظت كخطة حالية. ‎/report‎ يضيف الأخبار والرسم البياني.",
    },
    "term.factor.trend_structure": {"en": "Trend", "ar": "الاتجاه العام"},
    "term.factor.ema_cross": {"en": "EMA cross", "ar": "تقاطع المتوسطات"},
    "term.factor.ema_slope": {"en": "EMA slope", "ar": "ميل المتوسط"},
    "term.factor.macd": {"en": "MACD", "ar": "MACD"},
    "term.factor.rsi": {"en": "RSI", "ar": "RSI"},
    "term.factor.adx_direction": {"en": "ADX", "ar": "ADX"},
    "term.factor.range_position": {"en": "Range", "ar": "موقع النطاق"},
    "term.factor.momentum": {"en": "Momentum", "ar": "الزخم"},
    "now.hint": {
        "en": "Details for one: /now <symbol>",
        "ar": "للتفاصيل: ‎/now <الرمز>‎",
    },
    "now.failed": {
        "en": "{epic}: no live data ({error})",
        "ar": "{epic}: لا توجد بيانات مباشرة ({error})",
    },
    "now.price": {
        "en": "{epic}  Bid {bid} | Ask {ask} | Spread {spread}",
        "ar": "{epic}  بيع {bid} | شراء {ask} | الفارق {spread}",
    },
    "now.day": {
        "en": "Today: open {open}, {change} ({percent}%) | high {high} low {low}",
        "ar": "اليوم: الافتتاح {open}، {change} ({percent}%) | الأعلى {high} الأدنى {low}",
    },
    "now.levels": {
        "en": "TP1 {tp1}\nTP2 {tp2}\nTP3 {tp3}\nSL  {sl}",
        "ar": "TP1 {tp1}\nTP2 {tp2}\nTP3 {tp3}\nSL  {sl}",
    },
    "now.trades": {
        "en": "Your open trades ({count}):",
        "ar": "صفقاتك المفتوحة ({count}):",
    },
    "now.trade": {
        "en": "[{id}] {direction} {size} @ {entry}: {move} ({r}R) {money} | SL {sl}",
        "ar": "[{id}] {direction} {size} @ {entry}: {move} ({r}R) {money} | الوقف {sl}",
    },
    "command.help": {
        "en": (
            "/now [symbol]      full analysis on live prices, right now\n"
            "/status            open positions and ladder state\n"
            "/confirm <id>      start managing a detected position\n"
            "/decline <id>      leave a detected position alone\n"
            "/manage <id>       take over a position declined earlier\n"
            "/report [epic]     rebuild and send the full plan\n"
            "/plan [epic]       show the stored plan levels\n"
            "/close <id>        close the remaining size now\n"
            "/be <id>           move the stop to entry now\n"
            "/journal [days]    win rate, R per instrument, target hit rates\n"
            "/hold <id>         mute reversal alerts on one trade\n"
            "/lang en|ar        switch language\n"
            "/pause /resume     stop or restart all order modifications"
        ),
        "ar": (
            "‎/now [symbol]‎      تحليل كامل على الأسعار المباشرة الآن\n"
            "‎/status‎            المراكز المفتوحة ومراحل الإغلاق\n"
            "‎/confirm <id>‎      بدء إدارة مركز تم رصده\n"
            "‎/decline <id>‎      ترك المركز دون إدارة\n"
            "‎/manage <id>‎       إدارة مركز سبق رفضه\n"
            "‎/report [epic]‎     إعادة بناء الخطة وإرسالها\n"
            "‎/plan [epic]‎       عرض مستويات الخطة المحفوظة\n"
            "‎/close <id>‎        إغلاق الكمية المتبقية الآن\n"
            "‎/be <id>‎           نقل الوقف إلى سعر الدخول الآن\n"
            "‎/journal [أيام]‎    نسبة الربح والأداء لكل أداة\n"
            "‎/hold <id>‎         كتم تنبيهات الانعكاس لصفقة واحدة\n"
            "‎/lang en|ar‎        تغيير اللغة\n"
            "‎/pause /resume‎     إيقاف أو استئناف تعديل الأوامر"
        ),
    },

    # ---------------------------------------------------------------- journal
    "journal.heading": {
        "en": "Performance, last {days} days (R = multiples of risk)",
        "ar": "الأداء خلال آخر {days} يومًا (R = مضاعفات المخاطرة)",
    },
    "journal.epic": {
        "en": "{epic}   {trades} trades   {wins}W {losses}L   {total}R   avg {average}R",
        "ar": "{epic}   {trades} صفقة   {wins} رابحة {losses} خاسرة   {total}R   المتوسط {average}R",
    },
    "journal.stage": {
        "en": "{stage} {hits}/{trades} ({percent}%)",
        "ar": "{stage} {hits}/{trades} ({percent}%)",
    },
    "journal.total": {
        "en": "Total {trades} trades  {total}R  |  best {best}  |  worst {worst}",
        "ar": "الإجمالي {trades} صفقة  {total}R  |  الأفضل {best}  |  الأسوأ {worst}",
    },
    "journal.reversal": {
        "en": (
            "Reversal fired on {count} trades (avg {with_avg}R) vs {without} "
            "without (avg {without_avg}R)"
        ),
        "ar": (
            "تم رصد انعكاس في {count} صفقة (المتوسط {with_avg}R) مقابل {without} "
            "بدون انعكاس (المتوسط {without_avg}R)"
        ),
    },
    "journal.inferred": {
        "en": (
            "{count} exit(s) estimated from the last quote, not observed -- "
            "broker fees and slippage are not included anywhere here."
        ),
        "ar": (
            "{count} خروج تم تقديره من آخر سعر وليس مرصودًا فعليًا — ورسوم المنصة "
            "والانزلاق السعري غير محتسبة هنا إطلاقًا."
        ),
    },
    "journal.empty": {
        "en": (
            "No closed trades in the last {days} days. The journal fills itself "
            "as trades close; nothing is reconstructed afterwards."
        ),
        "ar": (
            "لا توجد صفقات مغلقة خلال آخر {days} يومًا. يُبنى السجل مع إغلاق "
            "الصفقات، ولا يمكن استرجاعه لاحقًا."
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
        "en": "Direction bias: {bias}  (signal strength {confidence}/100)",
        "ar": "الاتجاه المتوقع: {bias}  (قوة الإشارة {confidence}/100)",
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
    "report.style": {
        "en": "Style: {style} (about {hours} h) | levels {entry}, structure "
              "{structure}, managed on {management}",
        "ar": "النمط: {style} (حوالي {hours} ساعة) | المستويات {entry}، الهيكل "
              "{structure}، الإدارة على {management}",
    },
    "risk.ok": {
        "en": "Risk {percent}% of equity = max {max_money} {currency}: open {deals} x "
              "{lots} lots ({targets}) | loss at SL {loss} {currency} ({loss_percent}%)",
        "ar": "مخاطرة {percent}% من رأس المال = حد أقصى {max_money} {currency}: افتح "
              "{deals} × {lots} لوت ({targets}) | الخسارة عند الوقف {loss} {currency} "
              "({loss_percent}%)",
    },
    "risk.reduced": {
        "en": "Risk limit: {planned} deals at the {min_lot} minimum would be over "
              "{percent}% -- open {deals} x {lots} lots instead ({targets}) | loss at SL "
              "{loss} {currency} ({loss_percent}%)",
        "ar": "حد المخاطرة: {planned} صفقات بالحد الأدنى {min_lot} تتجاوز {percent}% -- "
              "افتح {deals} × {lots} لوت بدلًا منها ({targets}) | الخسارة عند الوقف "
              "{loss} {currency} ({loss_percent}%)",
    },
    "risk.rejected": {
        "en": "REJECTED -- RISK LIMIT: even 1 deal at the {min_lot} minimum loses "
              "{min_money} {currency} ({min_percent}%) at the stop, over your {percent}% "
              "({max_money} {currency}). Skip this trade.",
        "ar": "مرفوضة -- حد المخاطرة: حتى صفقة واحدة بالحد الأدنى {min_lot} تخسر "
              "{min_money} {currency} ({min_percent}%) عند الوقف، أكثر من {percent}% "
              "({max_money} {currency}). تجنّب هذه الصفقة.",
    },
    "risk.detail": {
        "en": "Ideal {ideal} lots total | broker min {min_lot}, step {step}, max {max_lot} "
              "| equity {equity} {currency}",
        "ar": "الحجم المثالي {ideal} لوت إجمالًا | الحد الأدنى للوسيط {min_lot}، الخطوة "
              "{step}، الحد الأقصى {max_lot} | رأس المال {equity} {currency}",
    },
    "risk.unchecked": {
        "en": "Risk at stop: NOT CHECKED (account or contract size unavailable) -- size "
              "this trade yourself",
        "ar": "المخاطرة عند الوقف: لم يتم التحقق (بيانات الحساب أو العقد غير متاحة) -- "
              "حدد الحجم بنفسك",
    },
    "risk.actual_ok": {
        "en": "Your {deals} deal(s) lose {money} at the stop = {percent}% of equity "
              "(limit {limit}%) -- within the limit",
        "ar": "صفقاتك ({deals}) تخسر {money} عند الوقف = {percent}% من رأس المال "
              "(الحد {limit}%) -- ضمن الحد",
    },
    "risk.actual_over": {
        "en": "WARNING: your {deals} deal(s) lose {money} at the stop = {percent}% of "
              "equity -- OVER your {limit}% limit. Consider closing a deal or a smaller size.",
        "ar": "تحذير: صفقاتك ({deals}) تخسر {money} عند الوقف = {percent}% من رأس المال "
              "-- أكثر من حدك {limit}%. فكّر في إغلاق صفقة أو تقليل الحجم.",
    },
    "report.strength_note": {
        "en": "Signal strength measures how strong and consistent the evidence is. "
              "It is not a probability of winning.",
        "ar": "قوة الإشارة تقيس قوة الأدلة واتساقها، وليست احتمال ربح الصفقة.",
    },
    "verdict.line": {"en": "VERDICT: {verdict} -- {reason}", "ar": "الحكم: {verdict} -- {reason}"},
    "verdict.approved": {
        "en": "direction, entry and risk all check out",
        "ar": "الاتجاه ونقطة الدخول والمخاطرة كلها سليمة",
    },
    "verdict.weak_entry": {
        "en": "the direction is clear but the entry is weak",
        "ar": "الاتجاه واضح لكن نقطة الدخول ضعيفة",
    },
    "verdict.poor_entry": {
        "en": "poor entry -- wait for a better location",
        "ar": "نقطة دخول سيئة -- انتظر موقعًا أفضل",
    },
    "verdict.no_direction": {
        "en": "no clear direction",
        "ar": "لا يوجد اتجاه واضح",
    },
    "verdict.risk_limit": {
        "en": "the risk limit cannot be met",
        "ar": "لا يمكن الالتزام بحد المخاطرة",
    },
    "verdict.risk_unchecked": {
        "en": "the risk check could not be made",
        "ar": "تعذّر التحقق من المخاطرة",
    },
    "term.verdict.approved": {"en": "APPROVED", "ar": "مقبولة"},
    "term.verdict.caution": {"en": "CAUTION", "ar": "بحذر"},
    "term.verdict.rejected": {"en": "REJECTED", "ar": "مرفوضة"},
    "quality.line": {
        "en": "Entry quality: {quality}{reasons}",
        "ar": "جودة نقطة الدخول: {quality}{reasons}",
    },
    "term.quality.good": {"en": "GOOD", "ar": "جيدة"},
    "term.quality.weak": {"en": "WEAK", "ar": "ضعيفة"},
    "term.quality.poor": {"en": "POOR", "ar": "سيئة"},
    "term.quality.none": {"en": "n/a (no direction)", "ar": "غير متاحة (لا اتجاه)"},
    "quality.flag.timeframe_conflict": {
        "en": "{timeframe} points the other way ({bias})",
        "ar": "الإطار {timeframe} في الاتجاه المعاكس ({bias})",
    },
    "quality.flag.timeframe_partial": {
        "en": "{timeframe} has no clear direction",
        "ar": "الإطار {timeframe} بلا اتجاه واضح",
    },
    "quality.flag.against_bias": {
        "en": "levels are for the side against the {bias} analysis",
        "ar": "المستويات للجهة المعاكسة للتحليل {bias}",
    },
    "quality.flag.extended": {
        "en": "price already stretched {atr} ATR from its average",
        "ar": "السعر ممتد بالفعل {atr} ATR عن متوسطه",
    },
    "quality.flag.near_level": {
        "en": "a level at {level} is in the way ({atr} ATR)",
        "ar": "يوجد مستوى عند {level} في الطريق ({atr} ATR)",
    },
    "quality.flag.rsi_stretched": {
        "en": "RSI {rsi} already stretched",
        "ar": "مؤشر RSI عند {rsi} ممتد بالفعل",
    },
    "quality.flag.tp1_pushed": {
        "en": "TP1 had to be pushed past the nearest level for enough reward",
        "ar": "دُفع الهدف الأول بعد أقرب مستوى للحصول على عائد كافٍ",
    },
    "quality.flag.wide_spread": {
        "en": "spread is {share} of the stop distance",
        "ar": "الفارق يساوي {share} من مسافة الوقف",
    },
    "quality.flag.risk_limit": {
        "en": "risk limit cannot be met",
        "ar": "لا يمكن الالتزام بحد المخاطرة",
    },
    "alignment.line": {
        "en": "Timeframes: {higher} {higher_bias} vs {entry} {bias} -> {alignment}",
        "ar": "الأطر الزمنية: {higher} {higher_bias} مقابل {entry} {bias} ← {alignment}",
    },
    "term.alignment.aligned": {"en": "ALIGNED", "ar": "متوافقة"},
    "term.alignment.partial": {"en": "PARTIAL", "ar": "توافق جزئي"},
    "term.alignment.conflict": {"en": "CONFLICT", "ar": "متعارضة"},
    "term.alignment.uncertain": {"en": "UNCERTAIN", "ar": "غير مؤكدة"},
    "gemini.heading": {
        "en": "SECOND OPINION -- Gemini ({model})",
        "ar": "الرأي الثاني -- Gemini ({model})",
    },
    "gemini.direction": {
        "en": "Direction: {direction} | entry {quality} | strength {strength}/100 | "
              "timeframes {alignment}",
        "ar": "الاتجاه: {direction} | الدخول {quality} | القوة {strength}/100 | "
              "الأطر الزمنية {alignment}",
    },
    "gemini.agreement.aligned": {
        "en": "Agreement with the bot: ALIGNED",
        "ar": "التوافق مع البوت: متوافق",
    },
    "gemini.agreement.conflict": {
        "en": "Agreement with the bot: CONFLICT (bot {bot}, Gemini {other})",
        "ar": "التوافق مع البوت: تعارض (البوت {bot}، Gemini {other})",
    },
    "gemini.agreement.uncertain": {
        "en": "Agreement with the bot: UNCERTAIN (bot {bot}, Gemini {other})",
        "ar": "التوافق مع البوت: غير مؤكد (البوت {bot}، Gemini {other})",
    },
    "gemini.fundamental": {
        "en": "Fundamentals: {bias} | news {news}{summary}",
        "ar": "العوامل الأساسية: {bias} | الأخبار {news}{summary}",
    },
    "gemini.reasons": {"en": "Why: {items}", "ar": "الأسباب: {items}"},
    "gemini.risks": {"en": "Concerns: {items}", "ar": "المخاوف: {items}"},
    "gemini.invalid": {"en": "Wrong if: {items}", "ar": "يُلغى إذا: {items}"},
    "gemini.sources": {"en": "News sources: {items}", "ar": "مصادر الأخبار: {items}"},
    "gemini.failed": {
        "en": "SECOND OPINION: Gemini did not respond -- {status}: {reason}. "
              "The bot's analysis above stands on its own.",
        "ar": "الرأي الثاني: لم يستجب Gemini -- {status}: {reason}. "
              "تحليل البوت أعلاه قائم بذاته.",
    },
    "gemini.disabled": {
        "en": "SECOND OPINION: off -- add GEMINI_API_KEY to .env to turn it on.",
        "ar": "الرأي الثاني: متوقف -- أضف GEMINI_API_KEY إلى ملف ‎.env‎ لتشغيله.",
    },
    "term.gemini.uncertain": {"en": "UNCERTAIN", "ar": "غير مؤكد"},
    "term.gemini.unavailable": {"en": "UNAVAILABLE", "ar": "غير متاحة"},
    "term.gemini.available": {"en": "AVAILABLE", "ar": "متاحة"},
    "term.gemini.good": {"en": "GOOD", "ar": "جيد"},
    "term.gemini.weak": {"en": "WEAK", "ar": "ضعيف"},
    "term.gemini.poor": {"en": "POOR", "ar": "سيئ"},
    "term.gemini.aligned": {"en": "ALIGNED", "ar": "متوافقة"},
    "term.gemini.partial": {"en": "PARTIAL", "ar": "جزئية"},
    "term.gemini.conflict": {"en": "CONFLICT", "ar": "متعارضة"},
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
        "en": "bias {old} -> {new} (signal strength {confidence})",
        "ar": "الاتجاه {old} ← {new} (قوة الإشارة {confidence})",
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
