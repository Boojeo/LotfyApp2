"""Assemble the daily analytical report: bias, three targets, one stop."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..broker.base import BrokerAdapter
from ..config import Config
from ..i18n import Translator
from ..models import Bias, Direction, TradePlan, utcnow
from ..util.timeframes import seconds as timeframe_seconds
from . import bias as bias_module
from . import fundamental as fundamental_module
from . import news as news_module
from . import quality as quality_module
from . import structure as structure_module
from . import style as style_module
from .levels import build_plan

log = logging.getLogger(__name__)

TECHNICAL_WEIGHT = 0.7
FUNDAMENTAL_WEIGHT = 0.3


@dataclass
class ReportBuilder:
    broker: BrokerAdapter
    config: Config
    news_provider: Optional[news_module.NewsProvider] = None

    def __post_init__(self) -> None:
        if self.news_provider is None:
            self.news_provider = news_module.build(self.config.news)

    # ------------------------------------------------------------------ build

    def build(
        self,
        epic: str,
        direction: Optional[Direction] = None,
        *,
        fundamentals: bool = True,
    ) -> TradePlan:
        """Analyse ``epic``.

        ``direction`` forces which side the levels are built for.  It is used
        when adopting a position that runs against the bias: the bias is still
        reported honestly, but targets and stop must belong to the side the
        user is actually on, or the stop lands on the wrong side of the entry.

        ``fundamentals=False`` skips the news fetch and the AI call -- the
        only parts that can take tens of seconds -- for an instant read.
        """
        analysis = self.config.analysis
        epic_config = self.config.epic_config(epic)
        choice = style_module.choose(self.broker, epic, self.config)
        profile = choice.profile

        structure = self.broker.candles(
            epic, profile.structure_timeframe, analysis.structure_lookback
        )
        entry = choice.candles.get(profile.entry_timeframe) or self.broker.candles(
            epic, profile.entry_timeframe, analysis.entry_lookback
        )
        if len(entry) < 60:
            raise ValueError(
                f"{epic}: only {len(entry)} {profile.entry_timeframe} candles returned; "
                "need at least 60 for an analytical read"
            )

        technical = bias_module.analyse(entry, analysis, management=self.config.management)

        headlines: List[Dict[str, Any]] = []
        if fundamentals:
            try:
                headlines = self.news_provider.fetch(
                    epic_config.news_query or epic_config.display or epic,
                    hours=analysis.news_lookback_hours,
                    limit=analysis.news_limit,
                )
            except Exception as exc:  # news is enrichment, never a hard dependency
                log.warning("%s: news fetch failed (%s); continuing technical-only",
                            epic, exc)
            fundamental = fundamental_module.analyse(
                epic, epic_config.display or epic, headlines, self.config.llm
            )
        else:
            fundamental = fundamental_module.FundamentalRead(
                bias=Bias.NEUTRAL, confidence=0.0,
                summary="Instant read: news and AI skipped.", source="skipped",
            )

        combined = _combine(technical, fundamental)
        direction = direction or combined.direction or (
            Direction.BUY if technical.score >= 0 else Direction.SELL
        )

        quote = self.broker.quote(epic)
        reference = quote.mid
        level_plan = build_plan(
            entry, reference, direction, analysis,
            atr_value=technical.atr or None, extra_candles=structure,
        )

        rules = self.broker.market_rules(epic)
        sizing = style_module.size_for_plan(
            self.broker, self.config, rules, abs(reference - level_plan.sl),
            spread=quote.spread,
        )

        # The higher timeframe gives context; it is read on its own and
        # compared, never averaged into the entry timeframe's score.
        higher = None
        if len(structure) >= 60:
            higher = bias_module.analyse(
                structure, analysis, management=self.config.management
            ).bias
        assessment = quality_module.assess(
            bias=combined, direction=direction, price=reference, atr=level_plan.atr,
            ema_fast=technical.ema_fast, rsi=technical.rsi, stop=level_plan.sl,
            tp1=level_plan.tp1, levels=level_plan.levels, spread=quote.spread,
            tp1_pushed=any(note.startswith("TP1 pushed") for note in level_plan.notes),
            higher_timeframe=profile.structure_timeframe, higher_bias=higher,
            risk_verdict=sizing.get("verdict") if sizing else None,
        )
        market = self._market_map(epic, entry, structure, reference, level_plan.atr)
        plan = TradePlan(
            epic=epic,
            created_at=utcnow(),
            bias=combined,
            direction=direction,
            confidence=_combined_confidence(technical, fundamental),
            reference_price=rules.round_price(reference),
            tp1=rules.round_price(level_plan.tp1),
            tp2=rules.round_price(level_plan.tp2),
            tp3=rules.round_price(level_plan.tp3),
            sl=rules.round_price(level_plan.sl),
            atr=level_plan.atr,
            technical=technical.to_dict(),
            fundamental=fundamental.to_dict(),
            levels=level_plan.levels,
            headlines=headlines,
            narrative=" ".join(level_plan.notes),
            advisory_only=combined is Bias.NEUTRAL,
            plan_id=f"{epic}-{utcnow():%Y%m%d}-{uuid.uuid4().hex[:6]}",
            style=choice.to_dict(),
            sizing=sizing,
            assessment=assessment.to_dict(),
            market=market,
        )
        return plan

    def _market_map(self, epic, entry, structure, price, atr_value) -> Dict[str, Any]:
        """Gaps, Fibonacci, S/R families, Wolfe, Camarilla -- enrichment only."""
        try:
            try:
                daily = self.broker.candles(epic, "D1", 5)
            except Exception as exc:  # no daily history: skip just the pivots
                log.warning("%s: daily candles unavailable (%s)", epic, exc)
                daily = []
            return structure_module.analyse(
                entry, structure, daily, price, atr_value, self.config.analysis
            )
        except Exception:  # the map must never cost you the plan
            log.exception("%s: market map failed; continuing without it", epic)
            return {}

    # ------------------------------------------------------------------ rendering

    def render_markdown(self, plan: TradePlan, t: Optional[Translator] = None) -> str:
        return render_markdown(plan, t)

    def render_text(self, plan: TradePlan, t: Optional[Translator] = None) -> str:
        return render_text(plan, t)


def _combine(
    technical: bias_module.TechnicalRead,
    fundamental: fundamental_module.FundamentalRead,
) -> Bias:
    fundamental_score = {
        Bias.BULLISH: 1.0, Bias.BEARISH: -1.0, Bias.NEUTRAL: 0.0
    }[fundamental.bias] * fundamental.confidence
    score = technical.score * TECHNICAL_WEIGHT + fundamental_score * FUNDAMENTAL_WEIGHT
    if score >= 20:
        return Bias.BULLISH
    if score <= -20:
        return Bias.BEARISH
    return Bias.NEUTRAL


def _combined_confidence(
    technical: bias_module.TechnicalRead,
    fundamental: fundamental_module.FundamentalRead,
) -> float:
    base = technical.confidence * TECHNICAL_WEIGHT + fundamental.confidence * FUNDAMENTAL_WEIGHT
    # Disagreement between the two reads is information: discount it.
    if (
        fundamental.bias is not Bias.NEUTRAL
        and technical.bias is not Bias.NEUTRAL
        and fundamental.bias is not technical.bias
    ):
        base *= 0.7
    return round(min(100.0, base), 1)


def render_markdown(plan: TradePlan, t: Optional[Translator] = None, tag: str = "") -> str:
    t = t or Translator()
    technical = plan.technical
    fundamental = plan.fundamental
    lines: List[str] = [
        t("tr.title", emoji="📉", tag=tag, symbol=plan.epic),
        f"_{plan.created_at:%Y-%m-%d %H:%M UTC}_",
        "",
        *report_sections(plan, t),
    ]
    lines += [
        "",
        f"_{plan.narrative}_",
        "",
        "## " + t("report.technical_heading"),
        "",
        t("report.technical_summary",
          score=technical.get("score"),
          bias=t.bias_name(technical.get("bias", "")),
          adx=technical.get("adx"),
          strength=t.strength_name(technical.get("strength", "")),
          rsi=technical.get("rsi")),
        "",
        f"| {t('report.col_factor')} | {t('report.col_value')} | "
        f"{t('report.col_weight')} | {t('report.col_detail')} |",
        "| --- | --- | --- | --- |",
    ]
    for factor in technical.get("factors", []):
        lines.append(
            f"| {factor['name']} | {factor['value']:+.2f} | {factor['weight']} | "
            f"{factor['detail']} |"
        )
    lines += [
        "",
        "## " + t("report.fundamental_heading"),
        "",
        "**" + t("report.fundamental_summary",
                 bias=t.bias_name(fundamental.get("bias", "")),
                 confidence=fundamental.get("confidence"),
                 source=fundamental.get("source")) + "**",
        "",
        fundamental.get("summary", ""),
    ]
    for label, key in (
        (t("report.drivers"), "drivers"),
        (t("report.risks"), "risks"),
        (t("report.catalysts"), "catalysts"),
    ):
        items = fundamental.get(key) or []
        if items:
            lines += ["", f"**{label}**"] + [f"- {item}" for item in items]

    key_levels = [level for level in plan.levels if level.is_liquidity][:5]
    if key_levels:
        lines += ["", "## " + t("report.liquidity_heading"), ""]
        lines += [
            f"- {level.price:.4f} ({level.label}, score {level.score:.2f})"
            for level in key_levels
        ]

    if plan.headlines:
        lines += ["", "## " + t("report.headlines_heading"), ""]
        for item in plan.headlines[:8]:
            title = item.get("title", "")
            url = item.get("url", "")
            lines.append(f"- [{title}]({url})" if url else f"- {title}")

    lines += ["", "_" + t("report.plan_id", id=plan.plan_id) + "_"]
    return "\n".join(lines)


_TREND_EMOJI = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➖"}
_VERDICT_EMOJI = {"APPROVED": "✅", "CAUTION": "⚠️", "REJECTED": "❌"}
_REVERSAL_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}
_REVERSAL_LEVELS = ((4, "high"), (2, "medium"), (0, "low"))

# Flags that make a turn against the trade more likely, and by how much.
_REVERSAL_POINTS = {
    "against_bias": 2, "timeframe_conflict": 2, "timeframe_partial": 1,
    "extended": 1, "rsi_stretched": 1, "near_level": 1,
}
ADX_WEAK = 20.0
LOSS_PER_REVERSAL_POINT = 0.04
LOSS_CAP = 0.95


def reversal_points(plan: TradePlan) -> int:
    """How many independent warning signs point at a turn against the trade."""
    flags = (plan.assessment or {}).get("flags", [])
    points = sum(_REVERSAL_POINTS.get(flag.get("key"), 0) for flag in flags)
    adx = plan.technical.get("adx")
    if isinstance(adx, (int, float)) and adx < ADX_WEAK:
        points += 1  # no real trend to carry the move
    return points


def reversal_level(plan: TradePlan) -> str:
    points = reversal_points(plan)
    return next(name for floor, name in _REVERSAL_LEVELS if points >= floor)


def loss_probability(plan: TradePlan) -> float:
    """Rough chance the stop is hit before TP1, 0..1.

    The baseline is the driftless random-walk answer -- stop distance over
    total distance -- which is exactly ``1 / (1 + R)`` for a TP1 of ``R``.
    Each reversal warning sign nudges it up. It is an estimate for sizing up
    the entry, not a forecast, and the message says so.
    """
    reward = plan.reward_risk(plan.tp1)
    base = 1.0 / (1.0 + reward) if reward > 0 else 0.5
    return min(LOSS_CAP, base + reversal_points(plan) * LOSS_PER_REVERSAL_POINT)


def _signal_quality(strength: float) -> str:
    return "strong" if strength >= 70 else "moderate" if strength >= 40 else "weak"


def _verdict_text(plan: TradePlan, t: Translator) -> List[str]:
    """Title, trend, verdict and the reasons behind it."""
    found = plan.assessment or {}
    verdict = found.get("verdict", "")
    lines = [
        t("tr.trend", emoji=_TREND_EMOJI.get(plan.bias.value, "➖"),
          direction=t.bias_name(plan.bias, arrow=False),
          strength=f"{plan.confidence:.0f}",
          quality=t.term("signal", _signal_quality(plan.confidence)))
        + (t("report.advisory_tag") if plan.advisory_only else ""),
    ]
    if verdict:
        lines.append(t("tr.verdict", emoji=_VERDICT_EMOJI.get(verdict, "⚠️"),
                       verdict=t.term("verdict", verdict),
                       reason=t(found.get("verdict_key") or "verdict.weak_entry")))
        reasons = [t(f"quality.flag.{flag['key']}", **flag.get("args", {}))
                   for flag in found.get("flags", [])]
        detail = t.semicolon.join(reasons) if reasons else plan.narrative
        if detail:
            lines.append(t("tr.reason", reason=detail))
    return lines


def _risk_text(plan: TradePlan, t: Translator) -> List[str]:
    sizing = plan.sizing or {}
    heading = t("tr.risk_heading")
    if not sizing or sizing.get("verdict") == "UNCHECKED":
        return [heading, t("tr.risk_unavailable"), t("tr.final", action=t("tr.action_manual"))]
    money = lambda key: f"{sizing.get(key, 0):,.2f}"  # noqa: E731
    currency = sizing.get("currency", "")
    over = sizing.get("min_lot_percent", 0) > sizing.get("risk_percent", 0)
    skip = (sizing.get("verdict") == "REJECTED"
            or (plan.assessment or {}).get("verdict") == "REJECTED"
            or not sizing.get("legs"))
    if skip:
        action = t("tr.action_skip")
    else:
        action = t("tr.action_open", deals=sizing.get("legs"), lots=sizing.get("per_leg"),
                   targets=" / ".join(sizing.get("targets") or []) or "-",
                   money=money("total_money"), currency=currency,
                   percent=f"{sizing.get('total_percent', 0):.2f}")
    return [
        heading,
        t("tr.equity", equity=money("equity"), currency=currency),
        t("tr.allowed_risk", percent=f"{sizing.get('risk_percent', 0):g}",
          money=money("max_money"), currency=currency),
        t("tr.min_loss", min_lot=sizing.get("broker_min"), money=money("min_lot_money"),
          currency=currency, percent=f"{sizing.get('min_lot_percent', 0):.2f}",
          limit=t("tr.limit_exceeds" if over else "tr.limit_within")),
        t("tr.ideal_size", ideal=f"{sizing.get('ideal_volume', 0):.4f}",
          min_lot=sizing.get("broker_min")),
        t("tr.final", action=action),
    ]


def _digits(price: float) -> int:
    text = repr(float(price))
    return min(5, len(text.split(".")[1].rstrip("0"))) if "." in text else 0


def _market_text(plan: TradePlan, t: Translator) -> List[str]:
    """Gaps, Fibonacci and the support/resistance families, read in plain words."""
    found = plan.market
    if not found:
        return []
    digits = max(_digits(plan.reference_price), 2)
    fmt = lambda value: f"{value:.{digits}f}"  # noqa: E731
    lines = [t("mm.heading")]

    gaps = found.get("gaps") or []
    if gaps:
        for gap in gaps[:2]:
            lines.append(t(
                "mm.gap", emoji="🟢" if gap["kind"] == "bullish" else "🔴",
                kind=t.term("gapkind", gap["kind"]), gtype=t.term("gaptype", gap["type"]),
                low=fmt(gap["low"]), high=fmt(gap["high"]),
                state=t.term("gapstate", gap["state"]), size=gap["size_atr"],
                meaning=t(f"mm.gap_meaning.{gap['type']}"),
            ))
    else:
        lines.append(t("mm.gap_none"))

    fib = found.get("fibonacci")
    if fib:
        levels = fib["levels"]
        retr = [lv for lv in levels if lv["kind"] == "retracement"]
        ext = next((lv for lv in levels if lv["kind"] == "extension"), None)
        lines.append(t(
            "mm.fib", direction=t.term("fibdir", fib["direction"]),
            start=fmt(fib["start"]), end=fmt(fib["end"]),
            depth=f"{max(fib['depth'], 0) * 100:.0f}",
            result=t(f"mm.fib.{fib['zone']}", ext=fmt(ext["price"]) if ext else "-"),
        ))
        below = [lv for lv in retr + levels[len(retr):] if lv["role"] == "support"]
        above = [lv for lv in retr + levels[len(retr):] if lv["role"] == "resistance"]
        label = lambda lv: f"{fmt(lv['price'])} ({lv['ratio'] * 100:.1f}%)"  # noqa: E731
        near = lambda group: label(min(group, key=lambda lv: abs(lv["price"] - plan.reference_price))) if group else "-"  # noqa: E731,E501
        lines.append(t("mm.fib_levels", support=near(below), resistance=near(above)))

    static = found.get("static") or {}
    describe = lambda zone: (  # noqa: E731
        f"{fmt(zone['price'])} (x{zone['touches']})" if zone else "-")
    lines.append(t("mm.static", support=describe(static.get("support")),
                   resistance=describe(static.get("resistance"))))

    dynamic = found.get("dynamic") or []
    if dynamic:
        lines.append(t("mm.dynamic", items=t.semicolon.join(
            f"{d['name']} {fmt(d['price'])} ({t.term('role', d['role'])})" for d in dynamic)))

    wolfe = found.get("wolfe")
    if wolfe:
        lines.append(t("mm.wolfe", emoji="🟢" if wolfe["direction"] == "bullish" else "🔴",
                       kind=t.term("gapkind", wolfe["direction"]),
                       entry=fmt(wolfe["entry"]), target=fmt(wolfe["target"])))
    else:
        lines.append(t("mm.wolfe_none"))

    cam = found.get("camarilla")
    if cam:
        lines.append(t("mm.camarilla", s3=fmt(cam["s3"]), s4=fmt(cam["s4"]),
                       r3=fmt(cam["r3"]), r4=fmt(cam["r4"]), pivot=fmt(cam["pivot"]),
                       result=t(f"mm.cam.{cam['zone']}")))
    return lines


def report_sections(plan: TradePlan, t: Translator) -> List[str]:
    """Everything under the title: verdict, reversal analysis, levels, capital."""
    style = plan.style or {}
    level = reversal_level(plan)
    chance = loss_probability(plan)
    lines = [
        *_verdict_text(plan, t),
        "", "---", "",
        t("tr.reversal_heading"),
        t("tr.reversal_rate", emoji=_REVERSAL_EMOJI[level], description=t(f"tr.reversal.{level}")),
        t("tr.loss_probability", text=t(
            "tr.loss_text", percent=f"{round(chance * 100 / 5) * 5:.0f}",
            emoji=_REVERSAL_EMOJI["high" if chance >= 0.6 else "medium" if chance >= 0.4 else "low"])),
        t("tr.technical", score=plan.technical.get("score"),
          strength=t.strength_name(plan.technical.get("strength", "")),
          news=t.bias_name(plan.fundamental.get("bias", ""), arrow=False)),
        "", "---", "",
        *(_market_text(plan, t) + ["", "---", ""] if plan.market else []),
        t("tr.levels_heading"),
    ]
    if style and style.get("name") != "custom":
        lines.append(t("tr.style", style=t.term("style", style.get("name", "")),
                       levels=style.get("entry", ""), structure=style.get("structure", ""),
                       managed=style.get("management", "")))
    lines += [
        t("tr.reference", value=plan.reference_price),
        t("tr.atr", value=f"{plan.atr:.4f}"),
        t("tr.risk_to_stop", value=f"{plan.risk:.4f}"),
        t("tr.sl", value=plan.sl),
        *(t("tr.tp", n=n, price=price, r=f"{plan.reward_risk(price):.2f}")
          for n, price in enumerate(plan.targets, start=1)),
        "", "---", "",
        *_risk_text(plan, t),
    ]
    return lines


def render_full(plan: TradePlan, t: Optional[Translator] = None, tag: str = "") -> str:
    """The long report: every section, for /full, /now and the report file.

    ``tag`` is the environment marker (e.g. ``[DEMO] ``) so it lands inside the
    title; notifiers skip their own prefix when the message already has it.
    """
    t = t or Translator()
    return "\n".join([t("tr.title", emoji="📉", tag=tag, symbol=plan.epic), "",
                      *report_sections(plan, t)])


# ------------------------------------------------------------------ short message

DEAL_TARGETS = {3: (1, 2, 3), 2: (1, 3), 1: (1,)}   # which target each deal closes at
SHORT_FLAGS = 3                                     # reasons shown in the short form


def deal_hours(plan: TradePlan) -> Optional[List[float]]:
    """Rough hours until each target (TP1..TP3), or None when unknown.

    Distance in ATRs becomes candles of the levels chart: a steady trend (high
    ADX) covers ground about linearly, a wandering market takes roughly the
    square of the distance.  An estimate for planning, not a forecast.
    """
    entry = (plan.style or {}).get("entry")
    if not entry or plan.atr <= 0:
        return None
    try:
        hours_per_bar = timeframe_seconds(entry) / 3600.0
    except Exception:
        return None
    adx = plan.technical.get("adx")
    trend = min(float(adx), 40.0) / 40.0 if isinstance(adx, (int, float)) else 0.5
    exponent = 2.0 - trend
    return [
        (abs(price - plan.reference_price) / plan.atr) ** exponent * hours_per_bar
        for price in plan.targets
    ]


def _hours_text(hours: float, t: Translator) -> str:
    return t("sm.hours_lt1") if hours < 1 else t("sm.hours", n=round(hours))


def _decision(plan: TradePlan, t: Translator) -> tuple:
    """(text, allowed) -- what to do with this trade, in one line."""
    sizing = plan.sizing or {}
    if not sizing or sizing.get("verdict") == "UNCHECKED":
        return t("tr.action_manual"), False
    skip = (sizing.get("verdict") == "REJECTED" or not sizing.get("legs")
            or (plan.assessment or {}).get("verdict") == "REJECTED")
    if skip:
        return t("tr.action_skip"), False
    side = t.term("act", plan.direction.value)
    return t("sm.open", side=side, deals=sizing["legs"], lots=sizing["per_leg"]), True


def _map_lines(plan: TradePlan, t: Translator) -> List[str]:
    found = plan.market
    if not found:
        return []
    digits = max(_digits(plan.reference_price), 2)
    fmt = lambda value: f"{value:.{digits}f}"  # noqa: E731
    lines: List[str] = []
    gaps = found.get("gaps") or []
    if gaps:
        gap = gaps[0]
        lines.append(t("sm.gap", emoji="🟢" if gap["kind"] == "bullish" else "🔴",
                       kind=t.term("gapkind", gap["kind"]), gtype=t.term("gaptype", gap["type"]),
                       low=fmt(gap["low"]), high=fmt(gap["high"]),
                       state=t.term("gapstate", gap["state"])))
    else:
        lines.append(t("sm.gap_none"))
    fib = found.get("fibonacci")
    wolfe = found.get("wolfe")
    lines.append(t(
        "sm.fib_wolfe",
        fib=(t("sm.fib", zone=t.term("fibzone", fib["zone"]),
               depth=f"{max(fib['depth'], 0) * 100:.0f}") if fib else t("sm.none")),
        wolfe=(t("sm.wolfe", kind=t.term("gapkind", wolfe["direction"]))
               if wolfe else t("sm.none")),
    ))
    static = found.get("static") or {}
    support, resistance = static.get("support"), static.get("resistance")
    dynamic = found.get("dynamic") or []
    ema = next((d for d in dynamic if d["name"] == "EMA20"), None)
    lines.append(t(
        "sm.sr", support=fmt(support["price"]) if support else "-",
        resistance=fmt(resistance["price"]) if resistance else "-",
        ema=(" | " + f"EMA20 {fmt(ema['price'])}") if ema else "",
    ))
    cam = found.get("camarilla")
    if cam:
        lines.append(t("sm.camarilla", s3=fmt(cam["s3"]), r3=fmt(cam["r3"]),
                       zone=t.term("camzone", cam["zone"])))
    return lines


def render_text(plan: TradePlan, t: Optional[Translator] = None, tag: str = "") -> str:
    """The short Telegram message: side, verdict, risk of reversal, map, levels, decision."""
    t = t or Translator()
    found = plan.assessment or {}
    verdict = found.get("verdict", "")
    level = reversal_level(plan)
    chance = loss_probability(plan)
    lines = [
        t("sm.title", tag=tag, symbol=plan.epic, deal=t.term("deal", plan.direction.value)),
        t("sm.trend", emoji=_TREND_EMOJI.get(plan.bias.value, "➖"),
          bias=t.bias_name(plan.bias, arrow=False), strength=f"{plan.confidence:.0f}")
        + (t("report.advisory_tag") if plan.advisory_only else ""),
    ]
    if verdict:
        lines.append(t("sm.verdict", emoji=_VERDICT_EMOJI.get(verdict, "⚠️"),
                       verdict=t.term("verdict", verdict),
                       reason=t(found.get("verdict_key") or "verdict.weak_entry")))
        reasons = [t(f"quality.flag.{flag['key']}", **flag.get("args", {}))
                   for flag in found.get("flags", [])[:SHORT_FLAGS]]
        if reasons:
            lines.append("↳ " + t.semicolon.join(reasons))
    lines.append(t("sm.reversal", emoji=_REVERSAL_EMOJI[level], level=t.term("reversal", level),
                   percent=f"{round(chance * 100 / 5) * 5:.0f}"))
    lines += _map_lines(plan, t)
    lines.append(t("sm.entry", entry=plan.reference_price, sl=plan.sl))

    decision, allowed = _decision(plan, t)
    hours = deal_hours(plan)
    legs = (plan.sizing or {}).get("legs")
    mapping = DEAL_TARGETS.get(legs) if allowed else None
    shown = mapping or (1, 2, 3)
    for deal_number, target in enumerate(shown, start=1):
        price = plan.targets[target - 1]
        line = dict(k=target, price=price, r=f"{plan.reward_risk(price):.1f}",
                    hours=_hours_text(hours[target - 1], t) if hours else "-")
        if mapping:
            lines.append(t("sm.deal", n=deal_number, **line))
        else:
            lines.append(t("sm.target", **line))
    sizing = plan.sizing or {}
    if sizing and sizing.get("verdict") != "UNCHECKED":
        lines.append(t("sm.risk", percent=f"{sizing.get('risk_percent', 0):g}",
                       money=f"{sizing.get('max_money', 0):,.0f}",
                       currency=sizing.get("currency", ""), decision=decision))
    else:
        lines.append(t("sm.decision", decision=decision))
    return "\n".join(lines)
