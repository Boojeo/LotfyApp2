"""Assemble the daily analytical report: bias, three targets, one stop."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..broker.base import BrokerAdapter
from ..config import Config
from ..models import Bias, Direction, TradePlan, utcnow
from . import bias as bias_module
from . import fundamental as fundamental_module
from . import news as news_module
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

    def build(self, epic: str) -> TradePlan:
        analysis = self.config.analysis
        epic_config = self.config.epic_config(epic)

        structure = self.broker.candles(
            epic, analysis.structure_timeframe, analysis.structure_lookback
        )
        entry = self.broker.candles(epic, analysis.entry_timeframe, analysis.entry_lookback)
        if len(entry) < 60:
            raise ValueError(
                f"{epic}: only {len(entry)} {analysis.entry_timeframe} candles returned; "
                "need at least 60 for an analytical read"
            )

        technical = bias_module.analyse(entry, analysis, management=self.config.management)

        headlines: List[Dict[str, Any]] = []
        try:
            headlines = self.news_provider.fetch(
                epic_config.news_query or epic_config.display or epic,
                hours=analysis.news_lookback_hours,
                limit=analysis.news_limit,
            )
        except Exception as exc:  # news is enrichment, never a hard dependency
            log.warning("%s: news fetch failed (%s); continuing technical-only", epic, exc)

        fundamental = fundamental_module.analyse(
            epic, epic_config.display or epic, headlines, self.config.llm
        )

        combined = _combine(technical, fundamental)
        direction = combined.direction or (
            Direction.BUY if technical.score >= 0 else Direction.SELL
        )

        quote = self.broker.quote(epic)
        reference = quote.mid
        level_plan = build_plan(
            entry, reference, direction, analysis,
            atr_value=technical.atr or None, extra_candles=structure,
        )

        rules = self.broker.market_rules(epic)
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
        )

    # ------------------------------------------------------------------ rendering

    def render_markdown(self, plan: TradePlan) -> str:
        return render_markdown(plan)

    def render_text(self, plan: TradePlan) -> str:
        return render_text(plan)


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


def _arrow(bias: Bias) -> str:
    return {Bias.BULLISH: "BULLISH", Bias.BEARISH: "BEARISH", Bias.NEUTRAL: "NEUTRAL"}[bias]


def render_markdown(plan: TradePlan) -> str:
    technical = plan.technical
    fundamental = plan.fundamental
    risk = plan.risk
    lines: List[str] = [
        f"# {plan.epic} -- daily plan {plan.created_at:%Y-%m-%d %H:%M UTC}",
        "",
        f"**Direction bias: {_arrow(plan.bias)}**  (confidence {plan.confidence:.0f}/100)",
    ]
    if plan.advisory_only:
        lines.append("")
        lines.append(
            "> Bias is NEUTRAL. Levels below are reference only -- the bot will still "
            "manage a position you open, but nothing here argues for taking one."
        )
    lines += [
        "",
        f"Reference price {plan.reference_price} | ATR {plan.atr:.4f} | "
        f"risk to stop {risk:.4f}",
        "",
        "## Levels",
        "",
        "| Level | Price | Distance | R multiple |",
        "| --- | --- | --- | --- |",
    ]
    for name, price in (("TP1", plan.tp1), ("TP2", plan.tp2), ("TP3", plan.tp3)):
        lines.append(
            f"| {name} | {price} | {abs(price - plan.reference_price):.4f} | "
            f"{plan.reward_risk(price):.2f}R |"
        )
    lines.append(f"| SL | {plan.sl} | {risk:.4f} | -1.00R |")
    lines += [
        "",
        f"_{plan.narrative}_",
        "",
        "## Technical",
        "",
        f"Score {technical.get('score')} ({technical.get('bias')}), "
        f"ADX {technical.get('adx')} ({technical.get('strength')}), "
        f"RSI {technical.get('rsi')}",
        "",
        "| Factor | Value | Weight | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for factor in technical.get("factors", []):
        lines.append(
            f"| {factor['name']} | {factor['value']:+.2f} | {factor['weight']} | "
            f"{factor['detail']} |"
        )
    lines += [
        "",
        "## Fundamental / news",
        "",
        f"**{fundamental.get('bias')}** (confidence {fundamental.get('confidence')}, "
        f"source `{fundamental.get('source')}`)",
        "",
        fundamental.get("summary", ""),
    ]
    for label, key in (("Drivers", "drivers"), ("Risks", "risks"), ("Catalysts", "catalysts")):
        items = fundamental.get(key) or []
        if items:
            lines += ["", f"**{label}**"] + [f"- {item}" for item in items]

    key_levels = [level for level in plan.levels if level.is_liquidity][:5]
    if key_levels:
        lines += ["", "## Liquidity zones", ""]
        lines += [
            f"- {level.price:.4f} ({level.label}, score {level.score:.2f})"
            for level in key_levels
        ]

    if plan.headlines:
        lines += ["", "## Headlines", ""]
        for item in plan.headlines[:8]:
            title = item.get("title", "")
            url = item.get("url", "")
            lines.append(f"- [{title}]({url})" if url else f"- {title}")

    lines += ["", f"_plan id: {plan.plan_id}_"]
    return "\n".join(lines)


def render_text(plan: TradePlan) -> str:
    """Compact form for chat notifications."""
    lines = [
        f"{plan.epic} -- {_arrow(plan.bias)} ({plan.confidence:.0f}/100)"
        + ("  [advisory only]" if plan.advisory_only else ""),
        f"ref {plan.reference_price}  ATR {plan.atr:.4f}",
        f"TP1 {plan.tp1}  ({plan.reward_risk(plan.tp1):.2f}R)",
        f"TP2 {plan.tp2}  ({plan.reward_risk(plan.tp2):.2f}R)",
        f"TP3 {plan.tp3}  ({plan.reward_risk(plan.tp3):.2f}R)",
        f"SL  {plan.sl}",
        "",
        f"tech {plan.technical.get('score')} / {plan.technical.get('strength')}"
        f" | news {plan.fundamental.get('bias')} ({plan.fundamental.get('source')})",
        plan.fundamental.get("summary", "")[:400],
    ]
    return "\n".join(lines)
