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
    "now.trades": {
        "en": "Your open trades ({count}):",
        "ar": "صفقاتك المفتوحة ({count}):",
    },
    "now.trade": {
        "en": "[{id}] {direction} {size} @ {entry}: {move} ({r}R) {money} | SL {sl}",
        "ar": "[{id}] {direction} {size} @ {entry}: {move} ({r}R) {money} | الوقف {sl}",
    },
    "command.short_on": {
        "en": "Short messages from now on, even when unsure or the risk is big. "
              "/full [symbol] still shows the long report. /msg_auto goes back to automatic.",
        "ar": "رسائل مختصرة من الآن، حتى عند عدم اليقين أو ارتفاع الخطر. "
              "‎/full [الرمز]‎ ما زال يعرض التقرير الكامل. ‎/msg_auto‎ يعيد الوضع التلقائي.",
    },
    "command.auto_on": {
        "en": "Automatic: short messages, and the long report only when the analysis is "
              "unsure or the risk is big. /short turns the long report off.",
        "ar": "تلقائي: رسائل مختصرة، والتقرير الكامل فقط عند عدم اليقين أو ارتفاع الخطر. "
              "‎/short‎ يوقف التقرير الكامل.",
    },
    "command.help": {
        "en": (
            "/now [symbol]      full analysis on live prices, right now\n"
            "/status            open positions and ladder state\n"
            "/confirm <id>      start managing a detected position\n"
            "/decline <id>      leave a detected position alone\n"
            "/manage <id>       take over a position declined earlier\n"
            "/report [epic]     rebuild and send the full plan\n"
            "/plan [epic]       show the stored plan, short\n"
            "/full [epic]       the stored plan with every section\n"
            "/plan30            30-day growth plan: status, table, export\n"
            "/plan_status       30-day plan status\n"
            "/exit [id]         exit advice with tested odds (HOLD / CLOSE / MOVE STOP)\n"
            "/do <id>           apply the advice for one deal, once\n"
            "/skip <id>         cancel the advice for one deal\n"
            "/targets <id> 2|3  T1-T2 only, or back to T1-T2-T3\n"
            "/protect [id]      what is on the broker's server vs what needs the app\n"
            "/afford [symbol]   which instruments your account can afford, and the equity needed\n"
            "/why [symbol] [days]  what has been stopping opportunities, from your own history\n"
            "/auto_on /auto_off turn automatic trading on or off\n"
            "/auto_status       automatic trading state and latest decisions\n"
            "/risk [1|2|3]      show or set the risk per trade (% of equity)\n"
            "/daily_report      today's results and risk\n"
            "/panic             close all managed trades (asks to confirm)\n"
            "/short             always send the short message\n"
            "/msg_auto          long report only when unsure or risky (default)\n"
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
            "‎/plan [epic]‎       عرض الخطة المحفوظة باختصار\n"
            "‎/full [epic]‎       الخطة المحفوظة بكل الأقسام\n"
            "‎/plan30‎            خطة النمو 30 يومًا: الحالة والجدول والتصدير\n"
            "‎/plan_status‎       حالة خطة 30 يومًا\n"
            "‎/exit [id]‎         نصيحة الخروج بالاحتمالات المُختبَرة (احتفاظ / إغلاق / تحريك الوقف)\n"
            "‎/do <id>‎           تنفيذ النصيحة لصفقة واحدة مرة واحدة\n"
            "‎/skip <id>‎         إلغاء النصيحة لصفقة واحدة\n"
            "‎/targets <id> 2|3‎  الهدفان 1 و2 فقط، أو العودة إلى الأهداف الثلاثة\n"
            "‎/protect [id]‎      ما هو على خادم الوسيط وما يحتاج التطبيق\n"
            "‎/afford [symbol]‎   الأدوات التي يتحملها حسابك ورأس المال اللازم\n"
            "‎/why [symbol] [days]‎  ما الذي يوقف الفرص، من سجلك أنت\n"
            "‎/auto_on /auto_off‎ تشغيل أو إيقاف التداول الآلي\n"
            "‎/auto_status‎       حالة التداول الآلي وآخر القرارات\n"
            "‎/risk [1|2|3]‎      عرض أو ضبط المخاطرة للصفقة (% من رأس المال)\n"
            "‎/daily_report‎      نتائج اليوم والمخاطرة\n"
            "‎/panic‎             إغلاق كل الصفقات المُدارة (يطلب التأكيد)\n"
            "‎/short‎             إرسال الرسالة المختصرة دائمًا\n"
            "‎/msg_auto‎          التقرير الكامل فقط عند عدم اليقين أو الخطر (الافتراضي)\n"
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
    "chart.tools": {"en": "Analysis tools", "ar": "أدوات التحليل"},
    "chart.tool.fib": {"en": "Fibonacci", "ar": "فيبوناتشي"},
    "chart.tool.gap": {"en": "Fair value gap", "ar": "فجوة القيمة العادلة"},
    "chart.tool.camarilla": {"en": "Camarilla pivots", "ar": "محاور كاماريلا"},
    "chart.tool.wolfe": {"en": "Wolfe wave", "ar": "موجة وولف"},
    "chart.tool.static": {"en": "Static S/R zones", "ar": "دعم ومقاومة ثابتة"},
    "chart.tool.dynamic": {"en": "dynamic S/R", "ar": "دعم ومقاومة متحركة"},
    "chart.unavailable": {
        "en": "Chart could not be drawn ({error}); the text plan above still applies.",
        "ar": "تعذّر رسم الشارت ({error})؛ الخطة النصية أعلاه سارية.",
    },

    # ---------------------------------------------------------------- report
    "report.title": {
        "en": "{epic} -- daily plan {timestamp}",
        "ar": "{epic} — الخطة اليومية {timestamp}",
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
    "term.alignment.aligned": {"en": "ALIGNED", "ar": "متوافقة"},
    "term.alignment.partial": {"en": "PARTIAL", "ar": "توافق جزئي"},
    "term.alignment.conflict": {"en": "CONFLICT", "ar": "متعارضة"},
    "term.alignment.uncertain": {"en": "UNCERTAIN", "ar": "غير مؤكدة"},
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
    "report.advisory_tag": {"en": "  [advisory only]", "ar": "  [للاسترشاد فقط]"},
    "report.failed": {
        "en": "{epic}: report failed -- {error}",
        "ar": "{epic}: تعذّر إنشاء التقرير — {error}",
    },
    "report.no_plan": {
        "en": "No stored plan for {epic}. Try /report {epic}.",
        "ar": "لا توجد خطة محفوظة لـ {epic}. جرّب ‎/report {epic}‎.",
    },
    # Automatic trading (/auto_on, /auto_off, /auto_status, /risk, /daily_report, /panic).
    "auto.dry_run": {"en": " (DRY RUN: orders are only logged)", "ar": " (تجريبي: تُسجَّل الأوامر فقط)"},
    "auto.forced_off": {
        "en": "Automatic trading is OFF after the restart: a live account never resumes "
              "by itself. Send /auto_on to turn it on for this session.",
        "ar": "التداول الآلي متوقف بعد إعادة التشغيل: الحساب الحقيقي لا يستأنف تلقائيًا. "
              "أرسل ‎/auto_on‎ لتشغيله في هذه الجلسة.",
    },
    "auto.pending_settled": {
        "en": "An order on {epic} was in flight when the bot stopped. Checked the account: "
              "{count} position(s) are open there now. No order is repeated.",
        "ar": "كان هناك أمر على {epic} قيد التنفيذ عند توقف البوت. فُحص الحساب: توجد "
              "{count} صفقة مفتوحة الآن. لن يُكرَّر أي أمر.",
    },
    "auto.already_on": {
        "en": "Automatic trading is already ON. /auto_status shows its state.",
        "ar": "التداول الآلي يعمل بالفعل. ‎/auto_status‎ يعرض حالته.",
    },
    "auto.not_enabled": {
        "en": "Automatic trading is switched off in the settings (auto.enabled: false). "
              "Set it to true in config.yaml and restart the bot first. Nothing was turned on.",
        "ar": "التداول الآلي معطّل في الإعدادات (auto.enabled: false). اجعله true في "
              "config.yaml وأعد تشغيل البوت أولًا. لم يتم تشغيل شيء.",
    },
    "auto.not_enabled_short": {
        "en": "⚠️ auto.enabled is false in the config, so /auto_on will refuse.",
        "ar": "⚠️ الإعداد auto.enabled يساوي false لذلك سيرفض ‎/auto_on‎.",
    },
    "auto.live_not_allowed": {
        "en": "This is a LIVE account and auto.allow_live is false. Automatic trading with "
              "real money needs that setting turned on by you in config.yaml. Nothing was "
              "turned on.",
        "ar": "هذا حساب حقيقي والإعداد auto.allow_live يساوي false. التداول الآلي بأموال حقيقية "
              "يحتاج أن تفعّل هذا الإعداد بنفسك في config.yaml. لم يتم تشغيل شيء.",
    },
    "auto.live_confirm": {
        "en": "⚠️ LIVE account: this will place REAL orders with real money. To confirm, "
              "send /auto_on {code} within {seconds} seconds.",
        "ar": "⚠️ حساب حقيقي: سيُرسل أوامر حقيقية بأموال حقيقية. للتأكيد أرسل "
              "‎/auto_on {code}‎ خلال {seconds} ثانية.",
    },
    "auto.bad_risk": {
        "en": "The risk per trade is {risk}%, which is not 1, 2 or 3. Choose one with "
              "/risk 1, /risk 2 or /risk 3 first. Nothing was turned on.",
        "ar": "نسبة المخاطرة للصفقة {risk}% وليست 1 أو 2 أو 3. اختر واحدة بـ ‎/risk 1‎ أو "
              "‎/risk 2‎ أو ‎/risk 3‎ أولًا. لم يتم تشغيل شيء.",
    },
    "auto.no_instruments": {
        "en": "No instruments to trade: the watchlist and auto.instruments are empty.",
        "ar": "لا توجد أدوات للتداول: قائمة المراقبة وauto.instruments فارغتان.",
    },
    "auto.algo_off": {
        "en": "MetaTrader 5's Algo Trading button is off, so orders would be refused. "
              "Turn it on, then send /auto_on again.",
        "ar": "زر Algo Trading في ميتاتريدر 5 مطفأ لذلك سترفض الأوامر. شغّله ثم أرسل "
              "‎/auto_on‎ مجددًا.",
    },
    "auto.no_hedging": {
        "en": "This account nets positions, so three separate deals cannot be held. Automatic "
              "trading needs a hedging account for the three-deal exit model.",
        "ar": "هذا الحساب يدمج الصفقات فلا يمكن الاحتفاظ بثلاث صفقات منفصلة. التداول الآلي "
              "يحتاج حساب تحوّط لنموذج الخروج بثلاث صفقات.",
    },
    "auto.broker_unreadable": {
        "en": "Could not check the broker account ({error}). Nothing was turned on.",
        "ar": "تعذّر فحص حساب الوسيط ({error}). لم يتم تشغيل شيء.",
    },
    "auto.on": {
        "en": "🤖 Automatic trading is ON ({env}){dry}.\n"
              "Risk {risk}% per trade | daily loss limit {limit}% | up to {cap} open deals\n"
              "Instruments: {instruments}\n"
              "Sessions: {start}–{end} UTC, Mon–Fri\n"
              "Only APPROVED, aligned, strong signals are traded, each with its stop set at "
              "the broker. /auto_off stops new entries at once; open trades keep being "
              "managed. /panic closes everything after a confirmation.",
        "ar": "🤖 التداول الآلي يعمل ({env}){dry}.\n"
              "المخاطرة {risk}% للصفقة | حد الخسارة اليومي {limit}% | حتى {cap} صفقات مفتوحة\n"
              "الأدوات: {instruments}\n"
              "الجلسات: {start}–{end} بتوقيت UTC، من الإثنين إلى الجمعة\n"
              "تُتداول فقط الإشارات المقبولة والمتوافقة والقوية، ولكل صفقة وقفها عند الوسيط. "
              "‎/auto_off‎ يوقف الدخول الجديد فورًا والصفقات المفتوحة تبقى مُدارة. "
              "‎/panic‎ يغلق كل شيء بعد التأكيد.",
    },
    "auto.off": {
        "en": "🛑 Automatic trading is OFF ({was}). No new entries will be made.\n"
              "Open trades are NOT closed: stops, targets, trailing and reversal alerts keep "
              "working as before, and you can still use /close <id>. /panic closes everything "
              "after a confirmation.",
        "ar": "🛑 التداول الآلي متوقف ({was}). لن تُفتح صفقات جديدة.\n"
              "الصفقات المفتوحة لا تُغلق: الوقف والأهداف والتتبع وتنبيهات الانعكاس تعمل كما "
              "كانت، ويمكنك استخدام ‎/close <id>‎. ‎/panic‎ يغلق كل شيء بعد التأكيد.",
    },
    "auto.was_on": {"en": "it was ON", "ar": "كان يعمل"},
    "auto.was_off": {"en": "it was already OFF", "ar": "كان متوقفًا بالفعل"},
    "auto.panic_confirm": {
        "en": "⚠️ /panic turns automatic trading OFF and CLOSES every trade the bot manages, "
              "now. To confirm, send /panic {code} within {seconds} seconds.",
        "ar": "⚠️ ‎/panic‎ يوقف التداول الآلي ويُغلق كل صفقة يديرها البوت الآن. للتأكيد أرسل "
              "‎/panic {code}‎ خلال {seconds} ثانية.",
    },
    "auto.panic_start": {
        "en": "🚨 Panic: automatic trading OFF. Closing {count} managed trade(s).",
        "ar": "🚨 طوارئ: التداول الآلي متوقف. إغلاق {count} صفقة مُدارة.",
    },
    "auto.panic_failed": {
        "en": "Could not close {id}: {error}. Close it yourself.",
        "ar": "تعذّر إغلاق {id}: {error}. أغلقها بنفسك.",
    },
    "auto.legacy": {
        "en": "Did you mean /auto_on, /auto_off or /auto_status for automatic trading, or "
              "/msg_auto for the message length? Nothing was changed.",
        "ar": "هل تقصد ‎/auto_on‎ أو ‎/auto_off‎ أو ‎/auto_status‎ للتداول الآلي، أو ‎/msg_auto‎ "
              "لطول الرسالة؟ لم يتغير شيء.",
    },
    "risk.cmd_usage": {
        "en": "Risk per trade can be 1, 2 or 3 (% of equity): /risk 1, /risk 2 or /risk 3. "
              "1% is the default; 2% and 3% are higher-risk settings that you choose, never "
              "applied automatically.",
        "ar": "المخاطرة للصفقة يمكن أن تكون 1 أو 2 أو 3 (% من رأس المال): ‎/risk 1‎ أو ‎/risk 2‎ أو "
              "‎/risk 3‎. الافتراضي 1%؛ و2% و3% إعدادات أعلى مخاطرة تختارها أنت ولا تُطبَّق تلقائيًا.",
    },
    "risk.cmd_set": {
        "en": "✅ Risk per trade set to {risk}% of equity.",
        "ar": "✅ تم ضبط المخاطرة للصفقة على {risk}% من رأس المال.",
    },
    "risk.source_saved": {
        "en": "chosen with /risk and saved in the database (config.yaml says {config}%; the "
              "saved choice wins)",
        "ar": "اخترتها بـ ‎/risk‎ ومحفوظة في قاعدة البيانات (config.yaml يقول {config}%؛ "
              "المحفوظة هي المعتمدة)",
    },
    "risk.source_config": {"en": "from config.yaml", "ar": "من config.yaml"},
    "risk.cmd_higher": {
        "en": "⚠️ {risk}% is a higher-risk setting. Your choice; /risk 1 returns to the default.",
        "ar": "⚠️ {risk}% إعداد أعلى مخاطرة. هذا اختيارك؛ ‎/risk 1‎ يعيد الافتراضي.",
    },
    "risk.cmd_status": {
        "en": "🎯 Risk per trade: {risk}% of equity = {money} (equity {equity}) — {source}.\n"
              "Costs counted in the lot size: commission {commission} per lot, slippage "
              "{slippage}, plus half the spread. Change with /risk 1|2|3.",
        "ar": "🎯 المخاطرة للصفقة: {risk}% من رأس المال = {money} (رأس المال {equity}) — {source}.\n"
              "التكاليف المحسوبة في حجم اللوت: عمولة {commission} لكل لوت، انزلاق {slippage}، "
              "ونصف الفارق السعري. للتغيير ‎/risk 1|2|3‎.",
    },
    "auto.dry_opened": {
        "en": "🤖 [DRY RUN] would open {side} {legs} x {lots} lots on {epic}, SL {sl}. "
              "Nothing was sent to the broker.",
        "ar": "🤖 [تجريبي] كان سيفتح {side} {legs} × {lots} لوت على {epic}، الوقف {sl}. "
              "لم يُرسل شيء إلى الوسيط.",
    },
    "auto.opened": {
        "en": "🤖 AUTO OPENED: {epic} — {side}\n"
              "{legs} x {lots} lots | entry {entry} | SL {sl} (distance {distance})\n"
              "TP1 {tp1} · TP2 {tp2} · TP3 {tp3}\n"
              "⚠️ Risk {risk}% = max {maxloss} {currency} | estimated loss at SL {estloss} {currency}",
        "ar": "🤖 فُتحت آليًا: {epic} — {side}\n"
              "{legs} × {lots} لوت | الدخول {entry} | الوقف {sl} (المسافة {distance})\n"
              "الهدف1 {tp1} · الهدف2 {tp2} · الهدف3 {tp3}\n"
              "⚠️ المخاطرة {risk}% = حد أقصى {maxloss} {currency} | الخسارة المقدرة عند الوقف "
              "{estloss} {currency}",
    },
    "auto.rejected": {
        "en": "🤖 {epic}: no automatic trade — {reason}",
        "ar": "🤖 {epic}: لا صفقة آلية — {reason}",
    },
    "auto.limit_reached": {
        "en": "🛑 {epic}: {reason}\nNo new automatic entries today; open trades are still managed.",
        "ar": "🛑 {epic}: {reason}\nلا دخول آلي جديد اليوم؛ الصفقات المفتوحة ما زالت تُدار.",
    },
    "auto.error": {
        "en": "❗ Automatic trading, {epic}: {error}",
        "ar": "❗ التداول الآلي، {epic}: {error}",
    },
    "auto.state_on": {"en": "ON", "ar": "يعمل"},
    "auto.state_off": {"en": "OFF", "ar": "متوقف"},
    "auto.session_open": {"en": "open now", "ar": "مفتوحة الآن"},
    "auto.session_closed": {"en": "closed now", "ar": "مغلقة الآن"},
    "auto.status": {
        "en": "🤖 Automatic trading: {state} ({env}){dry}\n"
              "Risk {risk}% per trade | strength at least {minimum}/100\n"
              "Instruments: {instruments}\n"
              "Session {start}–{end} UTC Mon–Fri: {session}\n"
              "Open deals {open} of {cap} | daily loss limit {limit}% ({used}% used)",
        "ar": "🤖 التداول الآلي: {state} ({env}){dry}\n"
              "المخاطرة {risk}% للصفقة | قوة الإشارة لا تقل عن {minimum}/100\n"
              "الأدوات: {instruments}\n"
              "الجلسة {start}–{end} UTC من الإثنين إلى الجمعة: {session}\n"
              "الصفقات المفتوحة {open} من {cap} | حد الخسارة اليومي {limit}% (مستخدم {used}%)",
    },
    "auto.status_blocked": {
        "en": "🛑 The daily loss limit is reached: no new entries until tomorrow.",
        "ar": "🛑 بلغت حد الخسارة اليومي: لا دخول جديد حتى الغد.",
    },
    "auto.recent": {"en": "Latest decisions:", "ar": "آخر القرارات:"},
    "auto.report_no_equity": {
        "en": "The account equity could not be read, so no daily figures are shown.",
        "ar": "تعذّرت قراءة رأس مال الحساب لذلك لا تُعرض أرقام اليوم.",
    },
    "auto.report_results_unknown": {
        "en": "Trades: result cannot be worked out (use /plan30 result)",
        "ar": "الصفقات: لا يمكن حساب النتيجة (استخدم ‎/plan30 result‎)",
    },
    "auto.report_results_none": {"en": "Trades: none closed today", "ar": "الصفقات: لا صفقات مغلقة اليوم"},
    "auto.report_results": {
        "en": "Closed trades: {trades} (won {wins} / lost {losses}) | net {net}{estimated}\n"
              "Costs {costs} | worst drop {dd}",
        "ar": "الصفقات المغلقة: {trades} (رابحة {wins} / خاسرة {losses}) | الصافي {net}{estimated}\n"
              "التكاليف {costs} | أكبر تراجع {dd}",
    },
    "auto.report": {
        "en": "📊 Daily report — {date}\n"
              "💰 Equity {start} → {equity} ({change}, {pct}%)\n"
              "{results}\n"
              "📈 Open: {held} of {cap} deals | floating {floating}\n"
              "🛑 Daily loss limit {limit}%: {used}% used\n"
              "🎯 Risk {risk}% per trade = max {maxrisk}\n"
              "🤖 Automatic trading {state}: orders {orders}, opened {opened}, rejected "
              "{rejected}, errors {errors}",
        "ar": "📊 التقرير اليومي — {date}\n"
              "💰 رأس المال {start} ← {equity} ({change}، {pct}%)\n"
              "{results}\n"
              "📈 المفتوح: {held} من {cap} صفقات | العائم {floating}\n"
              "🛑 حد الخسارة اليومي {limit}%: مستخدم {used}%\n"
              "🎯 المخاطرة {risk}% للصفقة = حد أقصى {maxrisk}\n"
              "🤖 التداول الآلي {state}: أوامر {orders}، مفتوحة {opened}، مرفوضة {rejected}، "
              "أخطاء {errors}",
    },
    "auto.reject.not_permitted": {"en": "not a permitted instrument", "ar": "أداة غير مسموح بها"},
    "auto.reject.outside_session": {
        "en": "outside the trading session ({start}–{end} UTC, Mon–Fri)",
        "ar": "خارج جلسة التداول ({start}–{end} UTC، من الإثنين إلى الجمعة)",
    },
    "auto.reject.equity_unknown": {
        "en": "the account equity could not be read, so nothing is opened",
        "ar": "تعذّرت قراءة رأس مال الحساب لذلك لن تُفتح صفقة",
    },
    "auto.reject.daily_limit": {
        "en": "daily loss limit reached: down {loss} today ({percent}% of the day's start, "
              "limit {limit}%)",
        "ar": "بلغت حد الخسارة اليومي: خسارة {loss} اليوم ({percent}% من بداية اليوم، "
              "الحد {limit}%)",
    },
    "auto.reject.data_error": {
        "en": "market or account data is missing ({error})",
        "ar": "بيانات السوق أو الحساب غير متوفرة ({error})",
    },
    "auto.reject.open_on_instrument": {
        "en": "a position on this instrument is already open",
        "ar": "توجد صفقة مفتوحة على هذه الأداة بالفعل",
    },
    "auto.reject.positions_full": {
        "en": "the open-deal cap is reached ({count} open, maximum {cap})",
        "ar": "بلغت الحد الأقصى للصفقات المفتوحة ({count} مفتوحة، الحد {cap})",
    },
    "auto.reject.order_pending": {
        "en": "an earlier order is not settled yet", "ar": "أمر سابق لم يُحسم بعد",
    },
    "auto.reject.no_plan": {
        "en": "the analysis failed ({error})", "ar": "فشل التحليل ({error})",
    },
    "auto.reject.no_direction": {"en": "no clear direction", "ar": "لا يوجد اتجاه واضح"},
    "auto.reject.against_bias": {
        "en": "the levels are for the side against the analysis",
        "ar": "المستويات للجهة المعاكسة للتحليل",
    },
    "auto.reject.not_approved": {
        "en": "the verdict is {verdict}, not APPROVED", "ar": "الحكم {verdict} وليس مقبولًا",
    },
    "auto.reject.weak_signal": {
        "en": "signal strength {strength}/100 is below {minimum}",
        "ar": "قوة الإشارة {strength}/100 أقل من {minimum}",
    },
    "auto.reject.not_aligned": {
        "en": "the higher timeframe is not aligned ({alignment})",
        "ar": "الإطار الأعلى غير متوافق ({alignment})",
    },
    "auto.reject.doubtful": {
        "en": "too uncertain or risky: {reasons}", "ar": "غير مؤكدة أو عالية الخطر: {reasons}",
    },
    "auto.reject.risk_unchecked": {
        "en": "the risk could not be checked", "ar": "تعذّر التحقق من المخاطرة",
    },
    "auto.reject.risk_rejected": {
        "en": "the risk limit cannot be met: even the broker's smallest lot loses {minimum} "
              "{currency} at the stop, over the {percent}% limit ({money} {currency}). The stop "
              "is not widened and the risk is not raised",
        "ar": "لا يمكن الالتزام بحد المخاطرة: حتى أصغر لوت عند الوسيط يخسر {minimum} "
              "{currency} عند الوقف، أكثر من حد {percent}% ({money} {currency}). لا يُوسَّع الوقف "
              "ولا تُرفع المخاطرة",
    },
    "auto.reject.risk_mismatch": {
        "en": "the plan was sized for a different risk % than the one selected",
        "ar": "حُسبت الخطة بنسبة مخاطرة مختلفة عن المختارة",
    },
    "auto.reject.not_tradeable": {
        "en": "the market is not tradeable right now", "ar": "السوق غير قابل للتداول الآن",
    },
    "auto.reject.cooldown": {
        "en": "this instrument and side was entered less than {hours} h ago",
        "ar": "دُخلت هذه الأداة والجهة قبل أقل من {hours} ساعة",
    },
    "auto.reject.order_failed": {
        "en": "the order failed after {opened} of {legs} deal(s) opened: {error}. Nothing is "
              "retried; check the account",
        "ar": "فشل الأمر بعد فتح {opened} من {legs} صفقات: {error}. لا إعادة محاولة؛ افحص الحساب",
    },
    # 30-day growth plan (/plan30).
    "p30.usage": {
        "en": "/plan30 | /plan30 start [capital] | /plan30 table | /plan30 result [day] <net> "
              "[note] | /plan30 note <text> | /plan30 export | /plan30 report",
        "ar": "‎/plan30‎ | ‎/plan30 start [رأس المال]‎ | ‎/plan30 table‎ | "
              "‎/plan30 result [اليوم] <الصافي> [ملاحظة]‎ | ‎/plan30 note <نص>‎ | "
              "‎/plan30 export‎ | ‎/plan30 report‎",
    },
    "p30.not_started": {
        "en": "No 30-day plan is running. /plan30 start [capital] starts one today "
              "(default 100).",
        "ar": "لا توجد خطة 30 يومًا قيد التشغيل. ‎/plan30 start [رأس المال]‎ تبدأ واحدة "
              "اليوم (الافتراضي 100).",
    },
    "p30.exists": {
        "en": "A plan has been running since {date}. Its history is preserved and cannot "
              "be restarted from chat.",
        "ar": "توجد خطة تعمل منذ {date}. سجلها محفوظ ولا يمكن إعادة تشغيلها من المحادثة.",
    },
    "p30.started": {
        "en": "📅 30-day plan started: capital {capital} {currency}, day 1 is {date}.\n"
              "Theoretical growth 25% a day is a maths scenario, not an expectation. "
              "Daily loss limit {percent}%, risk {risk}% per trade. Nothing is traded for "
              "you; this only tracks and warns.",
        "ar": "📅 بدأت خطة 30 يومًا: رأس المال {capital} {currency}، اليوم الأول {date}.\n"
              "النمو النظري 25% يوميًا سيناريو حسابي وليس توقعًا. حد الخسارة اليومي "
              "{percent}% والمخاطرة {risk}% للصفقة. لا يتم تنفيذ أي صفقة نيابة عنك؛ "
              "الخطة تتابع وتنبه فقط.",
    },
    "p30.status": {
        "en": "📅 30-Day Plan — Day {n} of {days} ({date})\n"
              "💰 Actual at start of day {actual} {currency} | Theoretical end of day {theory} {currency}\n"
              "📊 Today: {today}{limit}\n"
              "📈 Reviewed days {net} {currency} ({pct}%) | gap to the scenario {gap} {currency}\n"
              "⚠️ 25% a day is a maths scenario, not an expectation. Never force a trade.",
        "ar": "📅 خطة 30 يومًا — اليوم {n} من {days} ({date})\n"
              "💰 الفعلي بداية اليوم {actual} {currency} | النظري نهاية اليوم {theory} {currency}\n"
              "📊 اليوم: {today}{limit}\n"
              "📈 الأيام المراجعة {net} {currency} ({pct}%) | الفرق عن السيناريو {gap} {currency}\n"
              "⚠️ 25% يوميًا سيناريو حسابي وليس توقعًا. لا تُجبر نفسك على صفقة.",
    },
    "p30.today": {
        "en": "net {net} ({trades} trade(s){estimated})",
        "ar": "الصافي {net} ({trades} صفقة{estimated})",
    },
    "p30.today_none": {"en": "no trades yet", "ar": "لا صفقات بعد"},
    "p30.today_unknown": {
        "en": "waiting for a result (/plan30 result <net>)",
        "ar": "بانتظار النتيجة (‎/plan30 result <الصافي>‎)",
    },
    "p30.estimated": {"en": ", estimated", "ar": "، تقديري"},
    "p30.limit": {
        "en": "🛑 Daily loss limit {limit} {currency} ({used}% used)",
        "ar": "🛑 حد الخسارة اليومي {limit} {currency} (مستخدم {used}%)",
    },
    "p30.limit_hit": {
        "en": "🛑 Day {n}: the daily loss limit is reached ({net} of {limit} {currency}, "
              "{percent}% of the day's starting balance). Stop trading for today.",
        "ar": "🛑 اليوم {n}: بلغت حد الخسارة اليومي ({net} من {limit} {currency}، "
              "{percent}% من رصيد بداية اليوم). توقف عن التداول لهذا اليوم.",
    },
    "p30.pending": {
        "en": "Day {n} ({date}) has trades but no P/L the bot can work out. Record it with "
              "/plan30 result {n} <net profit or loss>; later days wait for it.",
        "ar": "اليوم {n} ({date}) فيه صفقات لكن لا يمكن حساب النتيجة. سجّلها بـ "
              "‎/plan30 result {n} <صافي الربح أو الخسارة>‎؛ الأيام التالية تنتظرها.",
    },
    "p30.pending_short": {
        "en": "⏳ Waiting for a result: day {days}", "ar": "⏳ بانتظار نتيجة اليوم: {days}",
    },
    "p30.table_head": {
        "en": "Day | date | actual start → end ({currency}) | net | theoretical start → end | "
              "25% hit | trades",
        "ar": "اليوم | التاريخ | الفعلي بداية ← نهاية ({currency}) | الصافي | النظري "
              "بداية ← نهاية | 25% | الصفقات",
    },
    "p30.table_row": {
        "en": "D{n} {date} | {a_start} → {a_end} | {net} | {t_start} → {t_end} | {mark} | {trades}",
        "ar": "ي{n} {date} | {a_start} ← {a_end} | {net} | {t_start} ← {t_end} | {mark} | {trades}",
    },
    "p30.table_foot": {
        "en": "Full 30 rows with formulas: /plan30 export", "ar": "الجدول الكامل بالمعادلات: ‎/plan30 export‎",
    },
    "p30.bad_day": {
        "en": "That day is not part of the plan yet (days 1–{days}, up to today).",
        "ar": "هذا اليوم ليس ضمن الخطة بعد (الأيام 1–{days} حتى اليوم).",
    },
    "p30.locked": {
        "en": "Day {n} has been reviewed and is preserved; it cannot be changed.",
        "ar": "اليوم {n} تمت مراجعته ومحفوظ ولا يمكن تغييره.",
    },
    "p30.recorded": {
        "en": "Day {n} result recorded: {net} {currency}.",
        "ar": "تم تسجيل نتيجة اليوم {n}: {net} {currency}.",
    },
    "p30.noted": {"en": "Note saved for day {n}.", "ar": "تم حفظ الملاحظة لليوم {n}."},
    "p30.no_excel": {
        "en": "Excel export needs the openpyxl package: run update.bat, then try again.",
        "ar": "تصدير إكسل يحتاج حزمة openpyxl: شغّل update.bat ثم أعد المحاولة.",
    },
    "p30.export_failed": {
        "en": "Could not write the spreadsheet: {error}", "ar": "تعذّر كتابة الجدول: {error}",
    },
    "p30.exported": {
        "en": "30-day plan spreadsheet (saved at {path})",
        "ar": "جدول خطة 30 يومًا (محفوظ في {path})",
    },
    "p30.in_progress": {"en": "in progress", "ar": "قيد التنفيذ"},
    "p30.no_trade_note": {
        "en": "No-trade day: no valid setup recorded.", "ar": "يوم بلا صفقات: لم تظهر فرصة صالحة.",
    },
    "p30.review": {
        "en": "📅 Day {n} review — {date}\n"
              "💰 {start} → {end} {currency} ({pct}%) | 25% target reached: {achieved}\n"
              "{body}\n"
              "🎯 Scenario for the day {theory} {currency} | gap {gap} {currency}\n"
              "📋 Rules: {rules}\n"
              "➡️ Next: {decision}",
        "ar": "📅 مراجعة اليوم {n} — {date}\n"
              "💰 {start} ← {end} {currency} ({pct}%) | بلوغ هدف 25%: {achieved}\n"
              "{body}\n"
              "🎯 السيناريو لليوم {theory} {currency} | الفرق {gap} {currency}\n"
              "📋 القواعد: {rules}\n"
              "➡️ التالي: {decision}",
    },
    "p30.review_trades": {
        "en": "🔢 Trades {trades} (won {wins} / lost {losses}) | costs {costs} | max drawdown {dd}",
        "ar": "🔢 الصفقات {trades} (رابحة {wins} / خاسرة {losses}) | التكاليف {costs} | "
              "أقصى تراجع {dd}",
    },
    "p30.review_no_trade": {
        "en": "🔢 No-trade day: no valid setup, nothing recorded or made up.",
        "ar": "🔢 يوم بلا صفقات: لا فرصة صالحة، ولم يُسجَّل أو يُختلق شيء.",
    },
    "p30.rules_ok": {"en": "all followed", "ar": "اتُّبعت كلها"},
    "p30.rules_broken": {"en": "broken — {rules}", "ar": "كُسرت — {rules}"},
    "p30.rule.loss_limit": {
        "en": "daily loss limit exceeded", "ar": "تجاوز حد الخسارة اليومي",
    },
    "p30.rule.trade_risk": {
        "en": "a trade lost more than the per-trade risk",
        "ar": "صفقة خسرت أكثر من حد المخاطرة للصفقة",
    },
    "p30.yes": {"en": "yes", "ar": "نعم"},
    "p30.no": {"en": "no", "ar": "لا"},
    "p30.decision.continue": {
        "en": "continue as planned", "ar": "المتابعة كما هو مخطط",
    },
    "p30.decision.reduce": {
        "en": "reduce risk until the rules hold again", "ar": "خفّض المخاطرة حتى تلتزم بالقواعد",
    },
    "p30.decision.pause": {
        "en": "pause trading and review before the next session",
        "ar": "أوقف التداول وراجع قبل الجلسة القادمة",
    },
    "p30.estimated_note": {
        "en": "ℹ️ Some profit was estimated (price move × lots × contract value); the broker did "
              "not book it. Correct it with /plan30 result if your statement differs.",
        "ar": "ℹ️ بعض الربح تقديري (حركة السعر × اللوت × قيمة العقد) لأن الوسيط لم يسجله. "
              "صحّحه بـ ‎/plan30 result‎ إن اختلف كشفك.",
    },
    "p30.final": {
        "en": "🏁 30-Day Report ({days}/{total} days recorded, {state})\n"
              "Initial {initial} → final actual {final} {currency}\n"
              "Net {net} {currency} ({pct}%)\n"
              "Theoretical final {theory} {currency} | difference {diff} {currency}\n"
              "Trades: won {wins}, lost {losses} (win rate {rate})\n"
              "Max drawdown {dd} (closed trades) | costs {costs} {currency}\n"
              "Rules followed on {compliant} of {reviewed} reviewed days; "
              "over the daily loss limit on {over}.\n"
              "This measures process, not a promised return.",
        "ar": "🏁 تقرير 30 يومًا ({days}/{total} يومًا مسجلًا، {state})\n"
              "البداية {initial} ← النهاية الفعلية {final} {currency}\n"
              "الصافي {net} {currency} ({pct}%)\n"
              "النهاية النظرية {theory} {currency} | الفرق {diff} {currency}\n"
              "الصفقات: رابحة {wins}، خاسرة {losses} (نسبة النجاح {rate})\n"
              "أقصى تراجع {dd} (الصفقات المغلقة) | التكاليف {costs} {currency}\n"
              "اتُّبعت القواعد في {compliant} من {reviewed} يومًا؛ "
              "تجاوز حد الخسارة اليومي في {over}.\n"
              "هذا يقيس الالتزام بالعملية وليس عائدًا مضمونًا.",
    },
    "p30.done": {"en": "complete", "ar": "مكتملة"},
    "p30.in_progress_state": {"en": "in progress", "ar": "قيد التنفيذ"},
    # Entry range, tested odds, and the one-line forms of both.
    "ez.none": {
        "en": "* **Entry Range:** none — the reference price already breaks the risk or "
              "reward rule, so there is no acceptable entry",
        "ar": "* **نطاق الدخول:** لا يوجد — سعر الدخول المرجعي يخالف قاعدة المخاطرة أو العائد "
              "لذلك لا يوجد دخول مقبول",
    },
    "ez.range": {
        "en": "* **Entry Range:** {low} – {high} (preferred {pref}) | the far end is set by: {limit}",
        "ar": "* **نطاق الدخول:** {low} – {high} (المفضل {pref}) | الطرف الأبعد يحدده: {limit}",
    },
    "ez.limit.risk": {
        "en": "the risk limit (the advised lots would lose more at the stop)",
        "ar": "حد المخاطرة (الحجم المقترح سيخسر أكثر عند الوقف)",
    },
    "ez.limit.reward_risk": {
        "en": "the minimum reward/risk of TP1", "ar": "الحد الأدنى للعائد إلى المخاطرة للهدف الأول",
    },
    "ez.tag.best": {"en": "best", "ar": "الأفضل"},
    "ez.tag.worst": {"en": "worst", "ar": "الأسوأ"},
    "ez.tag.mid": {"en": "·", "ar": "·"},
    "ez.tag.preferred": {"en": "preferred", "ar": "المفضل"},
    "ez.row_money": {
        "en": "  - {tag} {entry}: loss at SL -{loss} {currency} | {profits} | total {total} | "
              "R:R {rr}",
        "ar": "  - {tag} {entry}: الخسارة عند الوقف -{loss} {currency} | {profits} | الإجمالي "
              "{total} | R:R {rr}",
    },
    "ez.row_rr": {
        "en": "  - {tag} {entry}: reward/risk {rr}", "ar": "  - {tag} {entry}: العائد/المخاطرة {rr}",
    },
    "ez.invalid": {
        "en": "* **The range is no longer valid when:** price leaves it; TP1 is already passed or "
              "the stop touched; a wider spread pushes the loss over the risk limit or TP1 below "
              "{minimum}R; the plan is older than {hours} h; or a newer plan points the other way",
        "ar": "* **ينتهي صلاح النطاق عندما:** يخرج السعر منه؛ أو يتجاوز السعر الهدف الأول أو يلمس "
              "الوقف؛ أو يدفع اتساع الفارق الخسارة فوق حد المخاطرة أو الهدف الأول تحت {minimum}R؛ "
              "أو تتجاوز الخطة {hours} ساعة؛ أو تشير خطة أحدث إلى الاتجاه المعاكس",
    },
    "ez.lots": {
        "en": "* Lots are the plan's own ({legs} x {lots}) at every entry; a worse entry means a "
              "bigger loss at the same stop, never bigger lots",
        "ar": "* الحجم هو حجم الخطة ({legs} × {lots}) عند كل دخول؛ الدخول الأسوأ يعني خسارة أكبر "
              "عند الوقف نفسه، وليس حجمًا أكبر",
    },
    "ez.odds": {
        "en": "* **Tested odds** (reached before the stop): {hit} — {samples} test trades on "
              "{bars} bars of {tf}, {trend}, followed {horizon} bars each",
        "ar": "* **الاحتمالات المُختبَرة** (بلوغ الهدف قبل الوقف): {hit} — {samples} صفقة اختبار "
              "على {bars} شمعة {tf}، {trend}، تُتابَع كل منها {horizon} شمعة",
    },
    "ez.odds_stop": {
        "en": "* Stopped out first (reversal before the target): {stop}",
        "ar": "* ضُرب الوقف أولًا (انعكاس قبل الهدف): {stop}",
    },
    "ez.odds_note": {
        "en": "* These are measured frequencies on this instrument's own past candles for the "
              "same distances in ATR. They describe the past; they are not a forecast.",
        "ar": "* هذه نسب مقاسة على شموع هذه الأداة السابقة لنفس المسافات بوحدة ATR. تصف الماضي "
              "وليست توقعًا.",
    },
    "ez.odds_none": {
        "en": "* **Tested odds:** not enough history ({samples} test trades on {bars} bars; "
              "at least {minimum} are needed), so no number is shown",
        "ar": "* **الاحتمالات المُختبَرة:** التاريخ غير كافٍ ({samples} صفقة اختبار على {bars} "
              "شمعة؛ يلزم {minimum} على الأقل) لذلك لا يُعرض رقم",
    },
    "ez.trend_aligned": {
        "en": "using only entries trending the same way", "ar": "بالدخولات ذات الاتجاه المماثل فقط",
    },
    "ez.trend_all": {"en": "using all entries", "ar": "بكل الدخولات"},
    "sm.entry_range": {
        "en": "🎯 Entry {low}–{high} (prefer {pref}) | R:R {worst}→{best}",
        "ar": "🎯 الدخول {low}–{high} (المفضل {pref}) | R:R {worst}←{best}",
    },
    "sm.entry_none": {
        "en": "🎯 No acceptable entry range", "ar": "🎯 لا يوجد نطاق دخول مقبول",
    },
    "sm.odds": {
        "en": "🧪 Tested odds: {odds} (n={samples})", "ar": "🧪 الاحتمالات المُختبَرة: {odds} (n={samples})",
    },
    "sm.odds_none": {
        "en": "🧪 Tested odds: not enough history", "ar": "🧪 الاحتمالات المُختبَرة: التاريخ غير كافٍ",
    },
    # Broker-side protection and the reconnect audit.
    "prot.header_audit": {
        "en": "🛡 Protection check: a difference between the plan and the broker's server",
        "ar": "🛡 فحص الحماية: اختلاف بين الخطة وخادم الوسيط",
    },
    "prot.header_full": {
        "en": "🛡 Protection verified {reason}: {count} trade(s) re-read from the broker",
        "ar": "🛡 تم التحقق من الحماية {reason}: أُعيدت قراءة {count} صفقة من الوسيط",
    },
    "prot.reason.startup": {"en": "at start-up", "ar": "عند التشغيل"},
    "prot.reason.reconnect": {"en": "after the reconnect", "ar": "بعد عودة الاتصال"},
    "prot.reason.manual": {"en": "on request", "ar": "بناءً على طلبك"},
    "prot.audit_unreadable": {
        "en": "🛡 Protection could not be verified {reason}: the broker's positions cannot be "
              "read ({error}). The stop and target already on the broker's server are still "
              "active; monitoring resumes when the connection is back.",
        "ar": "🛡 تعذّر التحقق من الحماية {reason}: لا يمكن قراءة صفقات الوسيط ({error}). الوقف "
              "والهدف الموجودان على خادم الوسيط يعملان؛ تستأنف المراقبة عند عودة الاتصال.",
    },
    "prot.gone": {
        "en": "• {epic} [{id}]: no longer open at the broker (closed by its stop or target, or by "
              "you) — the journal is updated by the normal check",
        "ar": "• {epic} [{id}]: لم تعد مفتوحة عند الوسيط (أُغلقت بالوقف أو الهدف أو بيدك) — "
              "يُحدَّث السجل بالفحص المعتاد",
    },
    "prot.verified": {
        "en": "✅ {epic} [{id}]: stop {stop} and target {target} are confirmed on the broker's server",
        "ar": "✅ {epic} [{id}]: الوقف {stop} والهدف {target} مؤكدان على خادم الوسيط",
    },
    "prot.adopted": {
        "en": "• {epic} [{id}]: the broker holds a tighter stop ({level}) than the bot's — "
              "taken as the bot's own, not overridden",
        "ar": "• {epic} [{id}]: يحتفظ الوسيط بوقف أضيق ({level}) من وقف البوت — اعتُمد كوقف "
              "للبوت ولم يُلغَ",
    },
    "prot.target_different": {
        "en": "• {epic} [{id}]: the target on the broker is {actual}, the plan says {expected}. "
              "Not changed — it may be your own edit.",
        "ar": "• {epic} [{id}]: الهدف عند الوسيط {actual} والخطة تقول {expected}. لم يتغير — "
              "قد يكون تعديلك أنت.",
    },
    "prot.what.stop": {"en": "stop", "ar": "الوقف"},
    "prot.what.target": {"en": "target", "ar": "الهدف"},
    "prot.alert_paused": {
        "en": "⚠️ {epic} [{id}]: the {what} on the broker is {actual} but should be {expected}. "
              "Trading is paused, so nothing was changed — fix it yourself or send /resume.",
        "ar": "⚠️ {epic} [{id}]: {what} عند الوسيط {actual} ويجب أن يكون {expected}. التداول "
              "متوقف مؤقتًا لذلك لم يتغير شيء — أصلحه بنفسك أو أرسل ‎/resume‎.",
    },
    "prot.alert_dry": {
        "en": "⚠️ [dry run] {epic} [{id}]: the {what} on the broker is {actual} but should be "
              "{expected}. Nothing was sent.",
        "ar": "⚠️ [تجريبي] {epic} [{id}]: {what} عند الوسيط {actual} ويجب أن يكون {expected}. "
              "لم يُرسل شيء.",
    },
    "prot.gave_up": {
        "en": "❗ {epic} [{id}]: the {what} could not be put back after {n} attempt(s) (broker has "
              "{actual}, should be {expected}). The bot stops retrying — set it on the broker "
              "yourself.",
        "ar": "❗ {epic} [{id}]: تعذّر إعادة {what} بعد {n} محاولة (عند الوسيط {actual} والمطلوب "
              "{expected}). يتوقف البوت عن المحاولة — اضبطه على الوسيط بنفسك.",
    },
    "prot.repair_deferred": {
        "en": "⚠️ {epic} [{id}]: the {what} is {actual}, should be {expected}; the repair was "
              "deferred ({error}) and will be retried.",
        "ar": "⚠️ {epic} [{id}]: {what} هو {actual} والمطلوب {expected}؛ أُجّل الإصلاح ({error}) "
              "وستُعاد المحاولة.",
    },
    "prot.repair_refused": {
        "en": "❗ {epic} [{id}]: the broker refused the {what} {expected}: {error}",
        "ar": "❗ {epic} [{id}]: رفض الوسيط {what} {expected}: {error}",
    },
    "prot.repaired": {
        "en": "🔧 {epic} [{id}]: the {what} was {actual}, put back to {expected} and confirmed on "
              "the broker's server",
        "ar": "🔧 {epic} [{id}]: كان {what} {actual}، أُعيد إلى {expected} وتأكد على خادم الوسيط",
    },
    "prot.repair_unconfirmed": {
        "en": "❗ {epic} [{id}]: the {what} was sent as {expected} but the broker still shows "
              "{actual}. Check it.",
        "ar": "❗ {epic} [{id}]: أُرسل {what} {expected} لكن الوسيط ما زال يعرض {actual}. افحصه.",
    },
    "prot.nothing": {"en": "No managed trades to check.", "ar": "لا توجد صفقات مُدارة للفحص."},
    "prot.title": {
        "en": "🛡 {epic} [{id}] {side} {size}", "ar": "🛡 {epic} [{id}] {side} {size}",
    },
    "prot.broker_heading": {
        "en": "ON THE BROKER'S SERVER (still active if the app, this computer or the internet "
              "goes down):",
        "ar": "على خادم الوسيط (يبقى فعّالًا إن توقف التطبيق أو الجهاز أو الإنترنت):",
    },
    "prot.broker_stop": {
        "en": "  • Stop {level}: {what} {r}R{money} {state}",
        "ar": "  • الوقف {level}: {what} {r}R{money} {state}",
    },
    "prot.locks": {"en": "locks in", "ar": "يؤمّن"},
    "prot.risks": {"en": "risks", "ar": "يخاطر بـ"},
    "prot.broker_target": {
        "en": "  • Target {level} ({stage}) {state}", "ar": "  • الهدف {level} ({stage}) {state}",
    },
    "prot.state_ok": {"en": "✅ confirmed", "ar": "✅ مؤكد"},
    "prot.state_bad": {"en": "❌ differs from the plan", "ar": "❌ يختلف عن الخطة"},
    "prot.state_unknown": {"en": "❔ not readable now", "ar": "❔ غير مقروء الآن"},
    "prot.offline_note": {
        "en": "  Whatever stop and target the app last set stay here and keep protecting the "
              "trade while the app is off.",
        "ar": "  آخر وقف وهدف ضبطهما التطبيق يبقيان هنا ويحميان الصفقة أثناء توقف التطبيق.",
    },
    "prot.dynamic_heading": {
        "en": "NEEDS THE APP RUNNING (stops while it is off):",
        "ar": "يحتاج تشغيل التطبيق (يتوقف عند إيقافه):",
    },
    "prot.dyn_breakeven": {
        "en": "  • Stop to entry when {stage} is reached ({level})",
        "ar": "  • نقل الوقف إلى الدخول عند بلوغ {stage} ({level})",
    },
    "prot.dyn_risk_cut": {
        "en": "  • Halfway risk cut at {level}: only {left} of the risk is kept",
        "ar": "  • تخفيض المخاطرة في منتصف الطريق عند {level}: يبقى {left} فقط من المخاطرة",
    },
    "prot.dyn_trail": {
        "en": "  • Trailing stop after {stage} (ATR-based, moves with the trend)",
        "ar": "  • وقف متحرك بعد {stage} (مبني على ATR، يتحرك مع الاتجاه)",
    },
    "prot.dyn_reversal": {
        "en": "  • Reversal failsafe (action: {action})",
        "ar": "  • صمام الأمان عند الانعكاس (الإجراء: {action})",
    },
    "prot.dyn_extend": {
        "en": "  • TP3 extension in a strong trend", "ar": "  • تمديد الهدف الثالث في اتجاه قوي",
    },
    "prot.dyn_offline": {
        "en": "  If the app is off these simply do not happen; nothing is closed and the stop "
              "above stays where it is.",
        "ar": "  إن كان التطبيق متوقفًا فلن يحدث أي منها؛ لا يُغلق شيء ويبقى الوقف أعلاه مكانه.",
    },
    "prot.atr": {
        "en": "ATR reference (not applied): the stop is {gap} ATR behind price (ATR {atr}); a "
              "trailing stop now would sit at {trail}; a {tighten}-ATR lock at {lock}",
        "ar": "مرجع ATR (غير مطبّق): الوقف على بعد {gap} ATR خلف السعر (ATR {atr})؛ الوقف المتحرك "
              "الآن سيكون عند {trail}؛ وقفل {tighten} ATR عند {lock}",
    },
    "prot.limit_ok": {
        "en": "Loss if the stop is hit: {money} — within the risk limit.",
        "ar": "الخسارة إن ضُرب الوقف: {money} — ضمن حد المخاطرة.",
    },
    "prot.money_unknown": {
        "en": "The loss in money at the stop cannot be worked out: the broker publishes no "
              "contract value for this instrument.",
        "ar": "لا يمكن حساب الخسارة بالمال عند الوقف: الوسيط لا ينشر قيمة العقد لهذه الأداة.",
    },
    "prot.limit_over": {
        "en": "⚠️ Loss if the stop is hit: {money} per deal — the basket is OVER the risk limit "
              "(not changed by the bot).",
        "ar": "⚠️ الخسارة إن ضُرب الوقف: {money} للصفقة — السلة تتجاوز حد المخاطرة (لا يغيّرها البوت).",
    },
    # Exit advice.
    "adv.title": {
        "en": "🧭 Exit advice — {epic} [{id}] {side} (target {stage}), now {r}R",
        "ar": "🧭 نصيحة الخروج — {epic} [{id}] {side} (الهدف {stage})، الآن {r}R",
    },
    "adv.action": {"en": "Recommendation: {action}", "ar": "التوصية: {action}"},
    "adv.action.HOLD": {"en": "HOLD", "ar": "الاحتفاظ"},
    "adv.action.PARTIAL_CLOSE": {"en": "PARTIAL CLOSE", "ar": "إغلاق جزئي"},
    "adv.action.MOVE_STOP": {"en": "MOVE STOP LOSS", "ar": "تحريك وقف الخسارة"},
    "adv.action.CLOSE": {"en": "CLOSE", "ar": "الإغلاق"},
    "adv.detail.MOVE_STOP": {"en": "New stop: {level}", "ar": "الوقف الجديد: {level}"},
    "adv.detail.PARTIAL_CLOSE": {"en": "Close {size} lots", "ar": "إغلاق {size} لوت"},
    "adv.why": {"en": "Why:", "ar": "السبب:"},
    "adv.reason.close_reversal": {
        "en": "a trend reversal is confirmed ({agreeing} of the needed {required} independent "
              "signals, held for several checks) while you are {r}R in profit",
        "ar": "تأكد انعكاس الاتجاه ({agreeing} من {required} إشارات مستقلة مطلوبة، لعدة فحوصات) "
              "وأنت في ربح {r}R",
    },
    "adv.reason.close_ev": {
        "en": "in the historical test, holding to {stage} is worth {ev}R on average "
              "({samples} test trades), less than the {now}R already in hand",
        "ar": "في الاختبار التاريخي، الاحتفاظ حتى {stage} يساوي {ev}R في المتوسط "
              "({samples} صفقة اختبار)، أقل من {now}R المتحقق الآن",
    },
    "adv.reason.realistic_target": {
        "en": "the best-value target is {best} ({ev}R), not {far} ({far_ev}R)",
        "ar": "الهدف الأفضل قيمة هو {best} ({ev}R) وليس {far} ({far_ev}R)",
    },
    "adv.reason.move_stop": {
        "en": "momentum is fading and {agreeing} of {required} reversal signals are firing; "
              "a stop {atr} ATR behind price at {level} locks profit",
        "ar": "الزخم يضعف و{agreeing} من {required} إشارات انعكاس تعمل؛ وقف على بعد {atr} ATR "
              "خلف السعر عند {level} يؤمّن الربح",
    },
    "adv.reason.stop_not_improved": {
        "en": "a tighter stop at {level} would not improve on the current one (or the broker "
              "would not accept it)",
        "ar": "وقف أضيق عند {level} لن يحسّن الحالي (أو لن يقبله الوسيط)",
    },
    "adv.reason.hold": {
        "en": "nothing in the evidence justifies acting; the stop and targets already protect "
              "the trade ({r}R now)",
        "ar": "لا شيء في الأدلة يبرر التصرف؛ الوقف والأهداف تحمي الصفقة ({r}R الآن)",
    },
    "adv.reason.evidence": {
        "en": "evidence: {strength} trend (ADX {adx}), momentum {momentum}, reversal signals "
              "{agreeing}/{required}, nearest target {nearest} ATR away",
        "ar": "الأدلة: اتجاه {strength} (ADX {adx})، الزخم {momentum}، إشارات الانعكاس "
              "{agreeing}/{required}، أقرب هدف على بعد {nearest} ATR",
    },
    "adv.reason.no_odds": {
        "en": "no tested odds: only {samples} test trades on the history available (at least "
              "{minimum} are needed), so no number is used and this rests on the evidence above",
        "ar": "لا احتمالات مُختبَرة: {samples} صفقة اختبار فقط في التاريخ المتاح (يلزم {minimum} "
              "على الأقل) لذلك لا يُستخدم رقم وتعتمد التوصية على الأدلة أعلاه",
    },
    "adv.momentum.fading": {"en": "fading", "ar": "يضعف"},
    "adv.momentum.steady": {"en": "steady", "ar": "ثابت"},
    "adv.target": {
        "en": "  {stage} {level} ({dist} ATR away): reached before the stop in {p}% of test "
              "trades ({low}–{high}%), stopped first in {q}% | worth {ev}R vs {now}R in hand "
              "(n={n})",
        "ar": "  {stage} {level} (على بعد {dist} ATR): بلغه قبل الوقف {p}% من صفقات الاختبار "
              "({low}–{high}%)، وضُرب الوقف أولًا في {q}% | قيمته {ev}R مقابل {now}R متحققة "
              "(n={n})",
    },
    "adv.target_none": {
        "en": "  {stage} {level} ({dist} ATR away): no tested odds (not enough history)",
        "ar": "  {stage} {level} (على بعد {dist} ATR): لا احتمالات مُختبَرة (التاريخ غير كافٍ)",
    },
    "adv.best": {
        "en": "Most realistic target by tested value: {stage}",
        "ar": "الهدف الأكثر واقعية حسب القيمة المُختبَرة: {stage}",
    },
    "adv.honest": {
        "en": "These odds are measured frequencies on this instrument's past candles, not a "
              "forecast. Nothing happens unless you confirm.",
        "ar": "هذه الاحتمالات نسب مقاسة على شموع الأداة السابقة وليست توقعًا. لا يحدث شيء ما لم "
              "تؤكد.",
    },
    "adv.opt_do": {"en": "▶ /do {id} — apply: {action}", "ar": "▶ ‎/do {id}‎ — تنفيذ: {action}"},
    "adv.opt_skip": {"en": "✖ /skip {id} — cancel this advice", "ar": "✖ ‎/skip {id}‎ — إلغاء هذه النصيحة"},
    "adv.opt_targets": {
        "en": "🎯 /targets {id} 2 — T1–T2 only (final target pulled in to TP2) | /targets {id} 3 "
              "— back to T1–T2–T3",
        "ar": "🎯 ‎/targets {id} 2‎ — الهدفان 1 و2 فقط (الهدف النهائي يصبح الثاني) | "
              "‎/targets {id} 3‎ — العودة إلى الأهداف الثلاثة",
    },
    "adv.nothing": {"en": "No managed trades.", "ar": "لا توجد صفقات مُدارة."},
    "adv.unavailable": {
        "en": "{epic} [{id}]: the advice cannot be worked out now ({error}).",
        "ar": "{epic} [{id}]: تعذّر حساب النصيحة الآن ({error}).",
    },
    "adv.no_advice": {
        "en": "No advice is waiting for {id}. Send /exit {id} first.",
        "ar": "لا توجد نصيحة بانتظار {id}. أرسل ‎/exit {id}‎ أولًا.",
    },
    "adv.stale": {
        "en": "The advice for {id} is more than {minutes} minutes old. Send /exit {id} for a "
              "fresh one.",
        "ar": "نصيحة {id} أقدم من {minutes} دقيقة. أرسل ‎/exit {id}‎ للحصول على نصيحة جديدة.",
    },
    "adv.nothing_to_do": {
        "en": "The advice for {id} is HOLD: there is nothing to apply.",
        "ar": "نصيحة {id} هي الاحتفاظ: لا يوجد ما يُنفَّذ.",
    },
    "adv.changed": {
        "en": "The market moved: the advice for {id} is now {new}, not {old}. Nothing was done — "
              "read it and confirm again with /do {id}.",
        "ar": "تحرك السوق: نصيحة {id} أصبحت {new} وليست {old}. لم يتم شيء — اقرأها وأكد مجددًا "
              "بـ ‎/do {id}‎.",
    },
    "adv.not_accepted": {
        "en": "The broker did not accept it; check the logs.", "ar": "لم يقبله الوسيط؛ راجع السجلات.",
    },
    "adv.skipped": {
        "en": "Advice cancelled for {id} ({action}). You will be told again only if it changes.",
        "ar": "أُلغيت نصيحة {id} ({action}). ستُخطَر مجددًا فقط إن تغيّرت.",
    },
    "adv.targets_usage": {
        "en": "/targets <id> 2 — T1–T2 only (final target pulled in to TP2)\n"
              "/targets <id> 3 — back to T1–T2–T3",
        "ar": "‎/targets <id> 2‎ — الهدفان 1 و2 فقط (الهدف النهائي يصبح الثاني)\n"
              "‎/targets <id> 3‎ — العودة إلى الأهداف الثلاثة",
    },
    "adv.no_runner": {
        "en": "{id} has no final-target deal to change.", "ar": "لا توجد صفقة هدف نهائي لتغييرها في {id}.",
    },
    "adv.targets_already": {
        "en": "{id}: already set to {mode}.", "ar": "{id}: مضبوطة بالفعل على {mode}.",
    },
    "adv.targets_passed": {
        "en": "{id}: price {price} is already at or past {level}, so a target there cannot be "
              "set. Use /do {id} (or /close {id}) to take the profit now.",
        "ar": "{id}: السعر {price} عند {level} أو بعده لذلك لا يمكن ضبط هدف هناك. استخدم "
              "‎/do {id}‎ (أو ‎/close {id}‎) لأخذ الربح الآن.",
    },
    "adv.targets_set_2": {
        "en": "✅ {id}: T1–T2 only. The final deal's take-profit is now {level} (TP2), set on the "
              "broker's server. TP3 extension is off for it. /targets {id} 3 puts TP3 back.",
        "ar": "✅ {id}: الهدفان 1 و2 فقط. هدف الصفقة الأخيرة أصبح {level} (الثاني) على خادم "
              "الوسيط. تمديد الهدف الثالث متوقف لها. ‎/targets {id} 3‎ يعيد الثالث.",
    },
    "adv.targets_set_3": {
        "en": "✅ {id}: T1–T2–T3 again. The final deal's take-profit is back to {level} (TP3) "
              "on the broker's server.",
        "ar": "✅ {id}: الأهداف الثلاثة من جديد. هدف الصفقة الأخيرة عاد إلى {level} (الثالث) على "
              "خادم الوسيط.",
    },
    "reason.advice_stop": {
        "en": "stop moved to {level} as advised (confirmed by you)",
        "ar": "نُقل الوقف إلى {level} حسب النصيحة (بتأكيدك)",
    },
    "reason.advice_close": {
        "en": "closed as advised (confirmed by you)", "ar": "أُغلقت حسب النصيحة (بتأكيدك)",
    },
    "reason.advice_target": {
        "en": "final target set to {level} (confirmed by you)",
        "ar": "ضُبط الهدف النهائي على {level} (بتأكيدك)",
    },
    # Why an entry range is no longer valid.
    "ez.check.no_acceptable_entry": {
        "en": "there is no acceptable entry range", "ar": "لا يوجد نطاق دخول مقبول",
    },
    "ez.check.stop_touched": {
        "en": "price {price} has touched the stop {stop}", "ar": "لمس السعر {price} الوقف {stop}",
    },
    "ez.check.tp1_passed": {
        "en": "price {price} is already past TP1 {tp1}", "ar": "السعر {price} تجاوز الهدف الأول {tp1}",
    },
    "ez.check.outside_range": {
        "en": "price {price} is outside the range {low}–{high}",
        "ar": "السعر {price} خارج النطاق {low}–{high}",
    },
    "ez.check.risk_over_limit": {
        "en": "with today's spread the loss at the stop would be {loss}, over the {limit} limit",
        "ar": "بفارق السعر الحالي ستكون الخسارة عند الوقف {loss} وهي أكبر من الحد {limit}",
    },
    "ez.check.reward_risk_low": {
        "en": "TP1 would pay only {rr}R, below the {minimum}R minimum",
        "ar": "الهدف الأول سيدفع {rr}R فقط وهو أقل من الحد الأدنى {minimum}R",
    },
    "ez.check.expired": {
        "en": "the plan is older than {hours} h", "ar": "الخطة أقدم من {hours} ساعة",
    },
    "ez.check.bias_changed": {
        "en": "the analysis changed from {old} to {new}", "ar": "تغيّر التحليل من {old} إلى {new}",
    },
    "auto.reject.entry_invalid": {
        "en": "the entry range is no longer valid: {reasons}",
        "ar": "نطاق الدخول لم يعد صالحًا: {reasons}",
    },
    "auto.reject.entry_unchecked": {
        "en": "the entry range could not be worked out, so the entry cannot be checked",
        "ar": "تعذّر حساب نطاق الدخول لذلك لا يمكن فحص الدخول",
    },
    # What the account can afford (/afford, report lines, automatic-mode skip).
    "afford.none": {"en": "No instruments to check.", "ar": "لا توجد أدوات للفحص."},
    "afford.no_equity": {
        "en": "The account equity could not be read, so nothing can be worked out.",
        "ar": "تعذّرت قراءة رأس مال الحساب لذلك لا يمكن الحساب.",
    },
    "afford.heading": {
        "en": "💰 What your account can trade — equity {equity} USD, risk {risk}% = at most "
              "{money} lost per trade",
        "ar": "💰 ما يستطيع حسابك تداوله — رأس المال {equity} دولار، المخاطرة {risk}% = "
              "حد أقصى {money} خسارة للصفقة",
    },
    "afford.ok": {
        "en": "✅ {epic}: the smallest lot ({lot}) fits a stop up to {max_stop} ({n} ATR); the "
              "tightest stop the bot uses is {tight}",
        "ar": "✅ {epic}: أصغر لوت ({lot}) يناسب وقفًا حتى {max_stop} ({n} ATR)؛ أضيق وقف "
              "يستخدمه البوت {tight}",
    },
    "afford.no": {
        "en": "❌ {epic}: even the tightest stop the bot uses ({tight}) loses {loss} with the "
              "smallest lot ({lot}), over your {limit} limit — needs equity of about {need} at "
              "{risk}%",
        "ar": "❌ {epic}: حتى أضيق وقف يستخدمه البوت ({tight}) يخسر {loss} بأصغر لوت ({lot})، "
              "أكثر من حدك {limit} — يلزم رأس مال حوالي {need} عند {risk}%",
    },
    "afford.typical": {
        "en": " | your last plan's stop ({stop}) needs about {need}",
        "ar": " | وقف آخر خطة لك ({stop}) يلزمه حوالي {need}",
    },
    "afford.unknown": {
        "en": "❔ {epic}: cannot be judged ({why}); it is not skipped",
        "ar": "❔ {epic}: لا يمكن الحكم ({why})؛ لا يتم تخطيها",
    },
    "afford.why.no_value": {
        "en": "the broker publishes no contract value", "ar": "الوسيط لا ينشر قيمة العقد",
    },
    "afford.why.no_lot": {"en": "no minimum lot known", "ar": "الحد الأدنى للوت غير معروف"},
    "afford.why.no_atr": {"en": "no price history for the ATR", "ar": "لا يوجد تاريخ أسعار لحساب ATR"},
    "afford.why.no_equity": {"en": "no account equity", "ar": "لا يوجد رأس مال للحساب"},
    "afford.check_head": {
        "en": "Cross-check with the broker's own profit calculator (order_calc_profit), at the "
              "tightest stop, before spread and commission:",
        "ar": "مطابقة مع حاسبة الربح الخاصة بالوسيط (order_calc_profit) عند أضيق وقف، قبل "
              "الفارق والعمولة:",
    },
    "afford.check_ok": {
        "en": "  ✅ {epic}: {lot} lot, stop {stop}: the broker says it loses {theirs}; the bot's "
              "figure is {ours} ({gap}% apart)",
        "ar": "  ✅ {epic}: {lot} لوت، وقف {stop}: يقول الوسيط إنه يخسر {theirs}؛ رقم البوت "
              "{ours} (فرق {gap}%)",
    },
    "afford.check_bad": {
        "en": "  ⚠️ {epic}: {lot} lot, stop {stop}: the broker says it loses {theirs} but the "
              "bot's figure is {ours} ({gap}% apart) — the bot's contract value for this "
              "instrument may be wrong; do not trust its lot sizes for it until this is explained",
        "ar": "  ⚠️ {epic}: {lot} لوت، وقف {stop}: يقول الوسيط إنه يخسر {theirs} بينما رقم البوت "
              "{ours} (فرق {gap}%) — قد تكون قيمة العقد لدى البوت لهذه الأداة خاطئة؛ لا تثق بأحجام "
              "اللوت لها حتى يُفسَّر ذلك",
    },
    "afford.note": {
        "en": "This changes no rule: the risk limit, the minimum lot, the stop and the risk % "
              "are exactly as before. It only shows in advance what they will do. Automatic "
              "trading skips the ❌ instruments instead of analysing and rejecting them.",
        "ar": "لا يغيّر هذا أي قاعدة: حد المخاطرة وأصغر لوت والوقف ونسبة المخاطرة كما هي. "
              "يعرض فقط مسبقًا ما ستفعله. التداول الآلي يتخطى الأدوات ❌ بدل تحليلها ورفضها.",
    },
    "tr.min_equity": {
        "en": "* **Minimum equity for this stop** (smallest lot, {risk}% risk): about {need} "
              "{currency} — you have {have}",
        "ar": "* **أدنى رأس مال لهذا الوقف** (أصغر لوت، مخاطرة {risk}%): حوالي {need} {currency} — "
              "لديك {have}",
    },
    "tr.action_skip_needs": {
        "en": "Skip this trade — needs equity of about {need} {currency}",
        "ar": "تجنّب هذه الصفقة — تحتاج رأس مال حوالي {need} {currency}",
    },
    "auto.reject.unaffordable": {
        "en": "the account cannot afford it: the smallest lot loses {loss} at the tightest stop, "
              "over the {limit} limit (needs about {need} equity at {risk}%, you have {equity})",
        "ar": "الحساب لا يتحمل ذلك: أصغر لوت يخسر {loss} عند أضيق وقف، أكثر من الحد {limit} "
              "(يلزم رأس مال حوالي {need} عند {risk}%، ولديك {equity})",
    },
    "auto.none_affordable": {
        "en": "🤖 None of the instruments fits your account's risk limit (the smallest lot loses "
              "more than the limit at the tightest stop), so automatic trading has nothing to "
              "trade. /afford shows each one and the equity it needs. Nothing was changed.",
        "ar": "🤖 لا توجد أداة تناسب حد المخاطرة لحسابك (أصغر لوت يخسر أكثر من الحد عند أضيق "
              "وقف) لذلك لا يوجد ما يتداوله التداول الآلي. ‎/afford‎ يعرض كل أداة ورأس المال "
              "اللازم. لم يتغير شيء.",
    },
    # /why: what has been stopping opportunities.
    "why.none": {
        "en": "No evaluations are stored for the last {days} days yet.",
        "ar": "لا توجد تقييمات محفوظة لآخر {days} أيام بعد.",
    },
    "why.heading": {
        "en": "🔎 Why opportunities were not taken — last {days} days: {n} evaluations, "
              "{epics} instrument(s)",
        "ar": "🔎 لماذا لم تؤخذ الفرص — آخر {days} أيام: {n} تقييمًا، {epics} أداة",
    },
    "why.verdicts": {
        "en": "Manual verdicts: APPROVED {a} ({ap}) · CAUTION {c} ({cp}) · REJECTED {r} ({rp})",
        "ar": "أحكام الوضع اليدوي: مقبولة {a} ({ap}) · بحذر {c} ({cp}) · مرفوضة {r} ({rp})",
    },
    "why.neutral": {
        "en": "No clear direction at all: {n} ({p})", "ar": "بلا اتجاه واضح أصلًا: {n} ({p})",
    },
    "why.auto_head": {
        "en": "What stopped automatic mode (the first check that failed, in the order they run; "
              "minimum strength {minimum}):",
        "ar": "ما أوقف الوضع الآلي (أول فحص فشل، بترتيب تشغيلها؛ الحد الأدنى للقوة {minimum}):",
    },
    "why.manual_head": {
        "en": "What the report's verdict gave as its reason:",
        "ar": "السبب الذي ذكره حكم التقرير:",
    },
    "why.row": {"en": "  {rank}. {reason} — {n} ({p})", "ar": "  {rank}. {reason} — {n} ({p})"},
    "why.flags": {
        "en": "Entry-quality warnings present in these plans: {flags}",
        "ar": "تحذيرات جودة الدخول الموجودة في هذه الخطط: {flags}",
    },
    "why.strength": {
        "en": "Signal strength of directional plans: median {median}, best {top}; {ok} of {d} "
              "reach the {minimum} that automatic mode needs",
        "ar": "قوة الإشارة للخطط ذات الاتجاه: الوسيط {median}، الأعلى {top}؛ {ok} من {d} تبلغ "
              "{minimum} المطلوبة للوضع الآلي",
    },
    "why.per_epic": {"en": "Per instrument:", "ar": "لكل أداة:"},
    "why.epic_row": {
        "en": "  {epic}: {n} evaluations, {a} approved; most common blocker: {top} ({tn})",
        "ar": "  {epic}: {n} تقييمًا، {a} مقبولة؛ أكثر عائق: {top} ({tn})",
    },
    "why.note": {
        "en": "This counts decisions; it does not say whether they were right. "
              "`python -m tmbot audit` measures what happened afterwards on past candles.",
        "ar": "هذا يعدّ القرارات ولا يقول إن كانت صائبة. الأمر `python -m tmbot audit` يقيس ما "
              "حدث بعدها على الشموع السابقة.",
    },
    "why.gate.pass": {"en": "passed every plan-level check", "ar": "اجتازت كل فحوص الخطة"},
    "why.gate.no_direction": {"en": "no clear direction", "ar": "لا اتجاه واضح"},
    "why.gate.against_bias": {"en": "levels against the analysis", "ar": "المستويات ضد التحليل"},
    "why.gate.not_approved": {"en": "verdict is not APPROVED", "ar": "الحكم ليس مقبولًا"},
    "why.gate.weak_signal": {"en": "signal strength below the minimum", "ar": "قوة الإشارة أقل من الحد"},
    "why.gate.not_aligned": {"en": "higher timeframe not aligned", "ar": "الإطار الأعلى غير متوافق"},
    "why.gate.risk_unchecked": {"en": "risk could not be checked", "ar": "تعذّر فحص المخاطرة"},
    "why.gate.risk_rejected": {"en": "risk limit cannot be met", "ar": "لا يمكن الالتزام بحد المخاطرة"},
    "why.gate.doubtful": {"en": "flagged unsure or risky", "ar": "موسومة بعدم اليقين أو الخطر"},
    "why.gate.entry_range": {"en": "no acceptable entry range", "ar": "لا يوجد نطاق دخول مقبول"},
    # Why a message was sent in full.
    "detail.notice": {
        "en": "ℹ️ **Full report because:** {reasons}",
        "ar": "ℹ️ **تقرير كامل لأن:** {reasons}",
    },
    "detail.neutral": {"en": "there is no clear direction", "ar": "لا يوجد اتجاه واضح"},
    "detail.low_signal": {
        "en": "the signal is weak ({strength}/100)", "ar": "الإشارة ضعيفة ({strength}/100)",
    },
    "detail.conflict": {
        "en": "the {timeframe} chart disagrees", "ar": "الإطار {timeframe} يخالف الاتجاه",
    },
    "detail.no_higher": {
        "en": "the higher timeframe could not be read",
        "ar": "تعذّرت قراءة الإطار الزمني الأعلى",
    },
    "detail.reversal": {
        "en": "a reversal against the trade is likely",
        "ar": "انعكاس ضد الصفقة محتمل",
    },
    "detail.loss": {
        "en": "heuristic loss-risk score ~{percent}% (fixed rules, not a tested probability)",
        "ar": "درجة خطر الخسارة الإرشادية ~{percent}% (قواعد ثابتة وليست احتمالًا مُختبَرًا)",
    },
    "detail.risk_rejected": {
        "en": "the risk limit cannot be met", "ar": "لا يمكن الالتزام بحد المخاطرة",
    },
    "detail.risk_unchecked": {
        "en": "the risk could not be checked", "ar": "تعذّر التحقق من المخاطرة",
    },
    # Short Telegram message (render_text).
    "sm.title": {"en": "📉 {tag}{symbol} — {deal}", "ar": "📉 {tag}{symbol} — {deal}"},
    "term.deal.buy": {"en": "🟢 LONG (BUY)", "ar": "🟢 شراء (LONG)"},
    "term.deal.sell": {"en": "🔴 SHORT (SELL)", "ar": "🔴 بيع (SHORT)"},
    "term.act.buy": {"en": "Buy", "ar": "اشترِ"},
    "term.act.sell": {"en": "Sell", "ar": "بِع"},
    "sm.trend": {
        "en": "{emoji} Trend {bias} {strength}/100", "ar": "{emoji} الاتجاه {bias} {strength}/100",
    },
    "sm.verdict": {"en": "{emoji} {verdict} — {reason}", "ar": "{emoji} {verdict} — {reason}"},
    "sm.reversal": {
        "en": "🔄 Reversal {emoji} {level} | Loss-risk score ~{percent}% (heuristic)",
        "ar": "🔄 الانعكاس {emoji} {level} | درجة خطر الخسارة ~{percent}% (إرشادية)",
    },
    "term.reversal.low": {"en": "Low", "ar": "منخفض"},
    "term.reversal.medium": {"en": "Medium", "ar": "متوسط"},
    "term.reversal.high": {"en": "High", "ar": "مرتفع"},
    "sm.gap": {
        "en": "🧭 Gap: {emoji} {kind} {gtype} {low}–{high} ({state})",
        "ar": "🧭 الفجوة: {emoji} {gtype} {kind} {low}–{high} ({state})",
    },
    "sm.gap_none": {"en": "🧭 Gap: none", "ar": "🧭 الفجوة: لا يوجد"},
    "sm.fib_wolfe": {
        "en": "🧭 Fib: {fib} | Wolfe: {wolfe}", "ar": "🧭 فيبوناتشي: {fib} | وولف: {wolfe}",
    },
    "sm.fib": {"en": "{zone} {depth}%", "ar": "{zone} {depth}%"},
    "sm.wolfe": {"en": "{kind}", "ar": "{kind}"},
    "sm.none": {"en": "none", "ar": "لا يوجد"},
    "term.fibzone.shallow": {"en": "shallow", "ar": "ضحل"},
    "term.fibzone.moderate": {"en": "moderate", "ar": "طبيعي"},
    "term.fibzone.deep": {"en": "deep", "ar": "عميق"},
    "term.fibzone.golden": {"en": "golden pocket", "ar": "المنطقة الذهبية"},
    "term.fibzone.very_deep": {"en": "very deep", "ar": "عميق جدًا"},
    "term.fibzone.broken": {"en": "broken", "ar": "ملغاة"},
    "term.fibzone.extension": {"en": "extension", "ar": "امتداد"},
    "sm.sr": {
        "en": "🧭 S/R: {support} ◀ price ▶ {resistance}{ema}",
        "ar": "🧭 دعم/مقاومة: {support} ◀ السعر ▶ {resistance}{ema}",
    },
    "sm.camarilla": {
        "en": "🧭 Camarilla: S3 {s3} · R3 {r3} ({zone})",
        "ar": "🧭 كاماريلا: S3 {s3} · R3 {r3} ({zone})",
    },
    "term.camzone.range": {"en": "range", "ar": "نطاق"},
    "term.camzone.above_r3": {"en": "above R3", "ar": "فوق R3"},
    "term.camzone.above_r4": {"en": "above R4", "ar": "فوق R4"},
    "term.camzone.below_s3": {"en": "below S3", "ar": "تحت S3"},
    "term.camzone.below_s4": {"en": "below S4", "ar": "تحت S4"},
    "sm.entry": {
        "en": "📊 Entry {entry} | SL {sl} (distance {distance})",
        "ar": "📊 الدخول {entry} | الوقف {sl} (المسافة {distance})",
    },
    "sm.deal": {
        "en": "⏱ Deal {n} → TP{k} {price} ({r}R) {hours}",
        "ar": "⏱ الصفقة {n} ← الهدف {k} {price} ({r}R) {hours}",
    },
    "sm.target": {
        "en": "🎯 TP{k} {price} ({r}R) {hours}", "ar": "🎯 الهدف {k} {price} ({r}R) {hours}",
    },
    "sm.hours": {"en": "~{n} h", "ar": "~{n} ساعة"},
    "sm.hours_lt1": {"en": "<1 h", "ar": "أقل من ساعة"},
    "sm.open": {
        "en": "{side} {deals} x {lots} lots", "ar": "{side} {deals} × {lots} لوت",
    },
    "sm.risk": {
        "en": "⚠️ Risk {percent}% (max loss {money} {currency}) | est. loss at SL {est} "
              "{currency} | Decision: {decision}",
        "ar": "⚠️ المخاطرة {percent}% (حد الخسارة {money} {currency}) | الخسارة المقدرة عند "
              "الوقف {est} {currency} | القرار: {decision}",
    },
    "sm.risk_nolots": {
        "en": "⚠️ Risk {percent}% (max loss {money} {currency}) | no lots fit: the smallest lot "
              "({lot}) would lose {smallest} {currency} at the stop | Decision: {decision}",
        "ar": "⚠️ المخاطرة {percent}% (حد الخسارة {money} {currency}) | لا يوجد حجم مناسب: أصغر "
              "لوت ({lot}) سيخسر {smallest} {currency} عند الوقف | القرار: {decision}",
    },
    "sm.decision": {"en": "⚠️ Decision: {decision}", "ar": "⚠️ القرار: {decision}"},
    # Market map: gaps, Fibonacci, support/resistance families.
    "mm.heading": {
        "en": "### 🧭 **Market Map: Gaps, Fibonacci & Support/Resistance**",
        "ar": "### 🧭 **خريطة السوق: الفجوات وفيبوناتشي والدعم والمقاومة**",
    },
    "mm.gap": {
        "en": "* **Fair Value Gap:** {emoji} {kind} {gtype} gap {low} – {high} ({state}, "
              "{size} ATR) — {meaning}",
        "ar": "* **فجوة القيمة العادلة:** {emoji} فجوة {gtype} {kind} {low} – {high} "
              "({state}، {size} ATR) — {meaning}",
    },
    "mm.gap_none": {
        "en": "* **Fair Value Gap:** no open gap on the chart",
        "ar": "* **فجوة القيمة العادلة:** لا توجد فجوة مفتوحة على الشارت",
    },
    "mm.gap_meaning.breakaway": {
        "en": "price left a tight range; often starts a new trend",
        "ar": "السعر خرج من نطاق ضيق؛ غالبًا يبدأ اتجاهًا جديدًا",
    },
    "mm.gap_meaning.continuation": {
        "en": "inside a running trend; price often returns to fill part of it, then continues",
        "ar": "داخل اتجاه قائم؛ يعود السعر غالبًا لملء جزء منها ثم يكمل",
    },
    "mm.gap_meaning.exhaustion": {
        "en": "late in a stretched move; a warning that the trend may be running out",
        "ar": "في نهاية حركة ممتدة؛ تحذير من قرب انتهاء الاتجاه",
    },
    "mm.gap_meaning.common": {
        "en": "small gap with little meaning; usually filled quickly",
        "ar": "فجوة صغيرة قليلة الدلالة؛ تُملأ غالبًا بسرعة",
    },
    "term.gapkind.bullish": {"en": "bullish", "ar": "صاعدة"},
    "term.gapkind.bearish": {"en": "bearish", "ar": "هابطة"},
    "term.gaptype.breakaway": {"en": "breakaway", "ar": "اختراقية"},
    "term.gaptype.continuation": {"en": "continuation", "ar": "استمرارية"},
    "term.gaptype.exhaustion": {"en": "exhaustion", "ar": "استنزاف"},
    "term.gaptype.common": {"en": "common", "ar": "عادية"},
    "term.gapstate.open": {"en": "unfilled", "ar": "غير ممتلئة"},
    "term.gapstate.partial": {"en": "partly filled", "ar": "ممتلئة جزئيًا"},
    "term.fibdir.up": {"en": "up", "ar": "صاعدة"},
    "term.fibdir.down": {"en": "down", "ar": "هابطة"},
    "term.role.support": {"en": "support", "ar": "دعم"},
    "term.role.resistance": {"en": "resistance", "ar": "مقاومة"},
    "mm.fib": {
        "en": "* **Fibonacci ({direction} leg {start} → {end}):** price has retraced "
              "{depth}% — {result}",
        "ar": "* **فيبوناتشي (موجة {direction} {start} ← {end}):** ارتد السعر "
              "{depth}% — {result}",
    },
    "mm.fib.shallow": {
        "en": "a shallow pullback; the trend is strong and may resume soon",
        "ar": "تراجع ضحل؛ الاتجاه قوي وقد يستأنف قريبًا",
    },
    "mm.fib.moderate": {
        "en": "a normal pullback near 38.2–50%; a bounce here keeps the trend intact",
        "ar": "تراجع طبيعي قرب 38.2–50%؛ الارتداد هنا يُبقي الاتجاه سليمًا",
    },
    "mm.fib.deep": {
        "en": "a deep pullback (50–61.8%); the trend is weakening but not broken",
        "ar": "تراجع عميق (50–61.8%)؛ الاتجاه يضعف لكنه لم ينكسر",
    },
    "mm.fib.golden": {
        "en": "inside the golden pocket (61.8–78.6%), the most common reversal zone; "
              "if it holds the trend resumes",
        "ar": "داخل المنطقة الذهبية (61.8–78.6%) وهي أكثر مناطق الانعكاس شيوعًا؛ "
              "إن صمدت يستأنف الاتجاه",
    },
    "mm.fib.very_deep": {
        "en": "beyond 78.6%; the leg is close to being invalidated",
        "ar": "تجاوز 78.6%؛ الموجة قريبة من الإلغاء",
    },
    "mm.fib.broken": {
        "en": "beyond 100%; the leg is invalidated, do not trade it as a pullback",
        "ar": "تجاوز 100%؛ الموجة ملغاة، لا تتعامل معها كتراجع",
    },
    "mm.fib.extension": {
        "en": "price is past the end of the leg and in extension territory toward {ext}",
        "ar": "السعر تجاوز نهاية الموجة وهو في منطقة الامتداد نحو {ext}",
    },
    "mm.fib_levels": {
        "en": "* **Fibonacci Support/Resistance:** support {support} | resistance {resistance}",
        "ar": "* **دعم ومقاومة فيبوناتشي:** الدعم {support} | المقاومة {resistance}",
    },
    "mm.static": {
        "en": "* **Static Support/Resistance:** support {support} | resistance {resistance} "
              "(x = times tested)",
        "ar": "* **الدعم والمقاومة الثابتة:** الدعم {support} | المقاومة {resistance} "
              "(x = عدد مرات الاختبار)",
    },
    "mm.dynamic": {
        "en": "* **Dynamic (Moving) Support/Resistance:** {items}",
        "ar": "* **الدعم والمقاومة المتحركة:** {items}",
    },
    "mm.wolfe": {
        "en": "* **Wolfe Wave:** {emoji} {kind} pattern — entry zone {entry}, projected "
              "target {target}",
        "ar": "* **موجات وولف:** {emoji} نموذج {kind} — منطقة الدخول {entry}، "
              "الهدف المتوقع {target}",
    },
    "mm.wolfe_none": {
        "en": "* **Wolfe Wave:** no valid pattern right now",
        "ar": "* **موجات وولف:** لا يوجد نموذج صالح حاليًا",
    },
    "mm.camarilla": {
        "en": "* **Camarilla Pivots:** S3 {s3} | S4 {s4} | R3 {r3} | R4 {r4} (pivot {pivot}) "
              "— {result}",
        "ar": "* **محاور كاماريلا:** S3 {s3} | S4 {s4} | R3 {r3} | R4 {r4} "
              "(المحور {pivot}) — {result}",
    },
    "mm.cam.range": {
        "en": "between S3 and R3: a range day, S3/R3 are the edges to fade",
        "ar": "بين S3 وR3: يوم نطاق، وS3/R3 حدود يمكن التداول عكسها",
    },
    "mm.cam.above_r3": {
        "en": "above R3: strong buying, expect a rejection or a push to R4",
        "ar": "فوق R3: شراء قوي، توقع رفضًا أو اندفاعًا نحو R4",
    },
    "mm.cam.above_r4": {
        "en": "above R4: a breakout, buy the pullbacks",
        "ar": "فوق R4: اختراق، اشترِ عند التراجعات",
    },
    "mm.cam.below_s3": {
        "en": "below S3: strong selling, expect a rejection or a push to S4",
        "ar": "تحت S3: بيع قوي، توقع رفضًا أو اندفاعًا نحو S4",
    },
    "mm.cam.below_s4": {
        "en": "below S4: a breakdown, sell the bounces",
        "ar": "تحت S4: كسر هابط، بِع عند الارتدادات",
    },
    # Structured trade notification (render_text).
    "tr.title": {
        "en": "### {emoji} **{tag}{symbol} Trade Report**",
        "ar": "### {emoji} **{tag}تقرير صفقة {symbol}**",
    },
    "tr.trend": {
        "en": "* **Expected Trend:** {emoji} {direction} (Signal Strength: "
              "{strength}/100 - {quality})",
        "ar": "* **الاتجاه المتوقع:** {emoji} {direction} (قوة الإشارة: "
              "{strength}/100 - {quality})",
    },
    "tr.verdict": {
        "en": "* **Verdict:** {emoji} **{verdict}** — {reason}",
        "ar": "* **الحكم:** {emoji} **{verdict}** — {reason}",
    },
    "tr.reason": {"en": "* **Reason:** {reason}", "ar": "* **السبب:** {reason}"},
    "tr.reversal_heading": {
        "en": "### 🔄 **Reversal Analysis & Technical Indicators**",
        "ar": "### 🔄 **تحليل الانعكاس والمؤشرات الفنية**",
    },
    "tr.reversal_rate": {
        "en": "* **Expected Reversal Rate:** {emoji} {description}",
        "ar": "* **معدل الانعكاس المتوقع:** {emoji} {description}",
    },
    "tr.loss_probability": {
        "en": "* **Immediate Loss Risk (heuristic score):** {text}",
        "ar": "* **خطر الخسارة الفورية (درجة إرشادية):** {text}",
    },
    "tr.technical": {
        "en": "* **Technical Analysis:** {score} / {strength} | **News:** {news}",
        "ar": "* **التحليل الفني:** {score} / {strength} | **الأخبار:** {news}",
    },
    "tr.levels_heading": {
        "en": "### 📊 **Technical Levels Details**",
        "ar": "### 📊 **تفاصيل المستويات الفنية**",
    },
    "tr.style": {
        "en": "* **Style:** {style} | Levels: {levels}, Structure: {structure}, "
              "Managed on {managed}",
        "ar": "* **النمط:** {style} | المستويات: {levels}، الهيكل: {structure}، "
              "الإدارة على {managed}",
    },
    "tr.reference": {"en": "* **Reference Price:** {value}", "ar": "* **السعر المرجعي:** {value}"},
    "tr.atr": {
        "en": "* **Average True Range (ATR):** {value}",
        "ar": "* **متوسط المدى الحقيقي (ATR):** {value}",
    },
    "tr.risk_to_stop": {
        "en": "* **Risk to Stop:** {value}", "ar": "* **المخاطرة حتى الوقف:** {value}",
    },
    "tr.sl": {"en": "* **Stop Loss (SL):** {value}", "ar": "* **وقف الخسارة (SL):** {value}"},
    "tr.tp": {
        "en": "* **TP{n}:** {price} ({r}R)", "ar": "* **الهدف {n}:** {price} ({r}R)",
    },
    "tr.risk_heading": {
        "en": "### ⚠️ **Risk Management & Capital**",
        "ar": "### ⚠️ **إدارة المخاطر ورأس المال**",
    },
    "tr.equity": {
        "en": "* **Current Equity:** {equity} {currency}",
        "ar": "* **رأس المال الحالي:** {equity} {currency}",
    },
    "tr.allowed_risk": {
        "en": "* **Allowed Risk Limit ({percent}%):** {money} {currency}",
        "ar": "* **حد المخاطرة المسموح ({percent}%):** {money} {currency}",
    },
    "tr.min_loss": {
        "en": "* **Loss at Minimum Size ({min_lot} lot):** {money} {currency} "
              "(loses {percent}%, which {limit})",
        "ar": "* **الخسارة عند الحد الأدنى ({min_lot} لوت):** {money} {currency} "
              "(تخسر {percent}%، وهذا {limit})",
    },
    "tr.limit_exceeds": {"en": "exceeds your limit", "ar": "يتجاوز حدك"},
    "tr.limit_within": {"en": "is within your limit", "ar": "ضمن حدك"},
    "tr.ideal_size": {
        "en": "* **Ideal Size Suggested:** {ideal} lots total (Broker minimum is {min_lot})",
        "ar": "* **الحجم المثالي المقترح:** {ideal} لوت إجمالًا "
              "(الحد الأدنى للوسيط {min_lot})",
    },
    "tr.final": {"en": "* **Final Decision:** {action}", "ar": "* **القرار النهائي:** {action}"},
    "tr.action_open": {
        "en": "Open {deals} x {lots} lots ({targets}) — loss at SL {money} {currency} "
              "({percent}%)",
        "ar": "افتح {deals} × {lots} لوت ({targets}) — الخسارة عند الوقف {money} "
              "{currency} ({percent}%)",
    },
    "tr.action_skip": {"en": "Skip this trade", "ar": "تجنّب هذه الصفقة"},
    "tr.action_manual": {
        "en": "Risk could not be checked — size this trade yourself",
        "ar": "تعذّر التحقق من المخاطرة — حدد الحجم بنفسك",
    },
    "tr.risk_unavailable": {
        "en": "* Account or contract data unavailable — risk not checked",
        "ar": "* بيانات الحساب أو العقد غير متاحة — لم يتم التحقق من المخاطرة",
    },
    "tr.loss_text": {
        "en": "{emoji} heuristic score ~{percent}% for the stop being hit before TP1 "
              "(built from fixed rules, not a tested probability; the tested odds are below)",
        "ar": "{emoji} درجة إرشادية ~{percent}% لضرب الوقف قبل الهدف الأول "
              "(مبنية على قواعد ثابتة وليست احتمالًا مُختبَرًا؛ الاحتمالات المُختبَرة أدناه)",
    },
    "tr.reversal.low": {
        "en": "Low — trend evidence is consistent", "ar": "منخفض — أدلة الاتجاه متسقة",
    },
    "tr.reversal.medium": {
        "en": "Medium — some signs of a pullback", "ar": "متوسط — بعض علامات التراجع",
    },
    "tr.reversal.high": {
        "en": "High — a turn against this trade is likely",
        "ar": "مرتفع — انعكاس ضد هذه الصفقة محتمل",
    },
    "term.signal.strong": {"en": "Strong", "ar": "قوية"},
    "term.signal.moderate": {"en": "Moderate", "ar": "متوسطة"},
    "term.signal.weak": {"en": "Weak", "ar": "ضعيفة"},
    "report.update_heading": {"en": "{epic} plan update:", "ar": "تحديث خطة {epic}:"},
    "report.update_bias": {
        "en": "Expected Trend {old} -> {new} (Signal Strength: {confidence}/100)",
        "ar": "الاتجاه المتوقع {old} ← {new} (قوة الإشارة: {confidence}/100)",
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
