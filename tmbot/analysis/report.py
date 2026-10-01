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
from . import bias as bias_module
from . import fundamental as fundamental_module
from . import news as news_module
from . import quality as quality_module
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
        return TradePlan(
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
        )

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


def render_markdown(plan: TradePlan, t: Optional[Translator] = None) -> str:
    t = t or Translator()
    technical = plan.technical
    fundamental = plan.fundamental
    risk = plan.risk
    lines: List[str] = [
        "# " + t("report.title", epic=plan.epic,
                 timestamp=f"{plan.created_at:%Y-%m-%d %H:%M UTC}"),
        "",
        "**" + t("report.bias", bias=t.bias_name(plan.bias),
                 confidence=f"{plan.confidence:.0f}") + "**",
    ]
    judged = assessment_lines(plan, t)
    if judged:
        lines += [""] + ["**" + judged[0] + "**"] + judged[1:]
    lines += ["", "_" + t("report.strength_note") + "_"]
    styled = style_lines(plan, t)
    if styled:
        lines += [""] + styled
    if plan.advisory_only:
        lines.append("")
        lines.append("> " + t("report.advisory"))
    lines += [
        "",
        t("report.reference", price=plan.reference_price,
          atr=f"{plan.atr:.4f}", risk=f"{risk:.4f}"),
        "",
        "## " + t("report.levels_heading"),
        "",
        f"| {t('report.col_level')} | {t('report.col_price')} | "
        f"{t('report.col_distance')} | {t('report.col_r')} |",
        "| --- | --- | --- | --- |",
    ]
    for name, price in (("TP1", plan.tp1), ("TP2", plan.tp2), ("TP3", plan.tp3)):
        lines.append(
            f"| {name} | {price} | {abs(price - plan.reference_price):.4f} | "
            f"{plan.reward_risk(price):.2f}R |"
        )
    lines.append(f"| SL | {plan.sl} | {risk:.4f} | -1.00R |")
    sized = sizing_lines(plan, t)
    if sized:
        lines += [""] + ["**" + line + "**" for line in sized]
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


def style_lines(plan: TradePlan, t: Translator) -> List[str]:
    """Which trade style the levels are for, and why it was picked."""
    style = plan.style
    if not style or style.get("name") == "custom":
        return []
    lines = [t("report.style", style=t.term("style", style.get("name", "")),
               hours=style.get("hours", ""), entry=style.get("entry", ""),
               structure=style.get("structure", ""),
               management=style.get("management", ""))]
    if style.get("automatic") and style.get("reason_key"):
        lines.append(t(style["reason_key"], **(style.get("reason_args") or {})))
    return lines


def assessment_lines(plan: TradePlan, t: Translator) -> List[str]:
    """Verdict, entry quality and timeframe alignment -- kept apart from direction."""
    found = plan.assessment
    if not found:
        return []
    verdict = found.get("verdict", "")
    lines = [t("verdict.line", verdict=t.term("verdict", verdict),
               reason=t(found.get("verdict_key") or "verdict.weak_entry"))]
    reasons = [t(f"quality.flag.{flag['key']}", **flag.get("args", {}))
               for flag in found.get("flags", [])]
    lines.append(t("quality.line", quality=t.term("quality", found.get("quality", "")),
                   reasons=(" -- " + t.semicolon.join(reasons)) if reasons else ""))
    if found.get("higher_bias"):
        lines.append(t(
            "alignment.line", higher=found.get("higher_timeframe", ""),
            higher_bias=t.bias_name(found["higher_bias"]),
            entry=(plan.style or {}).get("entry", ""), bias=t.bias_name(plan.bias),
            alignment=t.term("alignment", found.get("alignment", "")),
        ))
    return lines


def sizing_lines(plan: TradePlan, t: Translator) -> List[str]:
    """Lots per deal that keep a stop-out to the configured share of the balance."""
    sizing = plan.sizing
    if not sizing:
        return []
    verdict = sizing.get("verdict")
    if verdict == "UNCHECKED":
        return [t("risk.unchecked")]
    money = lambda key: f"{sizing.get(key, 0):,.2f}"  # noqa: E731
    common = dict(
        percent=f"{sizing.get('risk_percent', 0):g}", currency=sizing.get("currency", ""),
        max_money=money("max_money"), lots=sizing.get("per_leg"), deals=sizing.get("legs"),
        planned=sizing.get("legs_planned"), loss=money("total_money"),
        loss_percent=f"{sizing.get('total_percent', 0):.2f}",
        min_lot=sizing.get("broker_min"), min_money=money("min_lot_money"),
        min_percent=f"{sizing.get('min_lot_percent', 0):.2f}",
        targets=" / ".join(sizing.get("targets") or []) or "-",
    )
    key = {"OK": "risk.ok", "REDUCED_LEGS": "risk.reduced",
           "REJECTED": "risk.rejected"}.get(verdict, "risk.ok")
    lines = [t(key, **common)]
    lines.append(t(
        "risk.detail", ideal=f"{sizing.get('ideal_volume', 0):.4f}",
        min_lot=sizing.get("broker_min"), step=sizing.get("broker_step"),
        max_lot=sizing.get("broker_max") or "-",
        equity=money("equity"), currency=sizing.get("currency", ""),
    ))
    return lines


def render_text(plan: TradePlan, t: Optional[Translator] = None) -> str:
    """Compact form for chat notifications."""
    t = t or Translator()
    lines = [
        t("report.bias", bias=t.bias_name(plan.bias), confidence=f"{plan.confidence:.0f}")
        + (t("report.advisory_tag") if plan.advisory_only else ""),
        *assessment_lines(plan, t),
        *style_lines(plan, t),
        t("report.reference", price=plan.reference_price,
          atr=f"{plan.atr:.4f}", risk=f"{plan.risk:.4f}"),
        f"TP1 {plan.tp1}  ({plan.reward_risk(plan.tp1):.2f}R)",
        f"TP2 {plan.tp2}  ({plan.reward_risk(plan.tp2):.2f}R)",
        f"TP3 {plan.tp3}  ({plan.reward_risk(plan.tp3):.2f}R)",
        f"SL  {plan.sl}",
        *sizing_lines(plan, t),
        "",
        t("report.tech_label",
          score=plan.technical.get("score"),
          strength=t.strength_name(plan.technical.get("strength", "")),
          bias=t.bias_name(plan.fundamental.get("bias", "")),
          source=plan.fundamental.get("source")),
        plan.fundamental.get("summary", "")[:400],
    ]
    return f"{plan.epic} -- " + "\n".join(lines)
