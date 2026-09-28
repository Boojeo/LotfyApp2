"""Trade style: how long a trade is planned for, and what size fits its stop.

The choice is a fixed rule, never a guess. The fastest allowed style is the
default. A slower one is chosen only when its own chart shows a strong trend
(ADX above a threshold) pointing the same way as the faster chart -- a slow
chart that is trending gives targets room to run; one that is ranging does
not, and wider levels there would only mean a wider stop.

Sizing turns the stop distance into lots: with a slower style the stop is
further away, so the same money at risk means fewer lots.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..broker.base import BrokerAdapter
from ..config import STYLE_PROFILES, Config, StyleProfile
from ..models import Bias, Candle, MarketRules
from . import bias as bias_module

log = logging.getLogger(__name__)

MIN_CANDLES = 60


@dataclass
class StyleChoice:
    profile: StyleProfile
    reason_key: str
    reason_args: Dict[str, Any] = field(default_factory=dict)
    # Candles already fetched while deciding, so the report need not refetch.
    candles: Dict[str, List[Candle]] = field(default_factory=dict)
    automatic: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.profile.name,
            "structure": self.profile.structure_timeframe,
            "entry": self.profile.entry_timeframe,
            "management": self.profile.management_timeframe,
            "hours": self.profile.hours,
            "automatic": self.automatic,
            "reason_key": self.reason_key,
            "reason_args": self.reason_args,
        }


def fixed_profile(config: Config) -> StyleProfile:
    """The single set of timeframes configured when styles are not automatic."""
    for profile in STYLE_PROFILES.values():
        if (
            profile.structure_timeframe == config.analysis.structure_timeframe
            and profile.entry_timeframe == config.analysis.entry_timeframe
            and profile.management_timeframe == config.management.management_timeframe
        ):
            return profile
    return StyleProfile(
        "custom",
        config.analysis.structure_timeframe,
        config.analysis.entry_timeframe,
        config.management.management_timeframe,
        "",
    )


def choose(broker: BrokerAdapter, epic: str, config: Config) -> StyleChoice:
    if config.style.mode != "auto":
        return StyleChoice(fixed_profile(config), "style.why_fixed")

    allowed = [STYLE_PROFILES[name] for name in STYLE_PROFILES if name in config.style.allowed]
    fastest = allowed[0]
    fetched: Dict[str, List[Candle]] = {}

    def read(timeframe: str) -> Optional[bias_module.TechnicalRead]:
        if timeframe not in fetched:
            fetched[timeframe] = broker.candles(
                epic, timeframe, config.analysis.entry_lookback
            )
        candles = fetched[timeframe]
        if len(candles) < MIN_CANDLES:
            return None
        return bias_module.analyse(candles, config.analysis, management=config.management)

    base = read(fastest.entry_timeframe)
    choice = StyleChoice(
        fastest, "style.why_only", {}, fetched, automatic=True
    )
    if base is None or len(allowed) == 1:
        return choice

    for slower in allowed[1:]:
        slow_read = read(slower.entry_timeframe)
        threshold = (
            config.style.swing_min_adx if slower.name == "swing"
            else config.style.intraday_min_adx
        )
        args = {
            "slow": slower.entry_timeframe,
            "fast": choice.profile.entry_timeframe,
            "adx": f"{slow_read.adx:.0f}" if slow_read else "-",
            "min": f"{threshold:.0f}",
        }
        if slow_read is None:
            choice.reason_key, choice.reason_args = "style.why_no_data", args
            break
        if slow_read.adx < threshold:
            choice.reason_key, choice.reason_args = "style.why_no_trend", args
            break
        if slow_read.bias is Bias.NEUTRAL or slow_read.bias is not base.bias:
            choice.reason_key, choice.reason_args = "style.why_disagree", args
            break
        choice.profile = slower
        choice.reason_key, choice.reason_args = "style.why_trend", args

    log.info("%s: style %s (%s %s)", epic, choice.profile.name,
             choice.reason_key, choice.reason_args)
    return choice


def _balance(summary: Dict[str, Any]) -> Optional[float]:
    value = summary.get("balance")
    if isinstance(value, dict):  # Capital.com nests it
        value = value.get("balance")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def size(
    *,
    balance: Optional[float],
    currency: str,
    risk_percent: float,
    stop_distance: float,
    rules: MarketRules,
    deals: int,
) -> Dict[str, Any]:
    """Lots per deal that lose ``risk_percent`` of the balance at the stop.

    Rounded DOWN to the broker's lot step, so the advice never risks more
    than asked -- except when even the minimum lot does, which is reported
    with the real percentage instead of being hidden.
    """
    value = rules.value_per_point
    if not (balance and balance > 0 and risk_percent > 0 and stop_distance > 0 and value):
        return {}
    deals = max(1, deals)
    risk_money = balance * risk_percent / 100.0
    loss_per_lot = stop_distance * value
    per_deal = risk_money / loss_per_lot / deals

    step = rules.size_step or rules.min_deal_size or 0.01
    per_deal = math.floor(round(per_deal / step, 9)) * step
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    per_deal = round(per_deal, decimals)

    minimum = rules.min_deal_size or step
    too_small = per_deal < minimum
    if too_small:
        per_deal = minimum
    actual_money = per_deal * deals * loss_per_lot
    return {
        "risk_percent": risk_percent,
        "risk_money": round(risk_money, 2),
        "currency": currency,
        "per_deal": per_deal,
        "deals": deals,
        "actual_percent": round(actual_money / balance * 100.0, 2),
        "actual_money": round(actual_money, 2),
        "above_target": too_small,
    }


def size_for_plan(
    broker: BrokerAdapter, config: Config, rules: MarketRules, stop_distance: float
) -> Dict[str, Any]:
    if config.report.risk_percent <= 0 or not rules.value_per_point:
        return {}
    try:
        summary = broker.account_summary()
    except Exception as exc:  # advice only; never block a report on it
        log.warning("no account balance for lot sizing (%s)", exc)
        return {}
    deals = len(config.management.leg_targets) if (
        config.management.exit_model == "three_deals"
    ) else 1
    return size(
        balance=_balance(summary),
        currency=str(summary.get("currency", "") or ""),
        risk_percent=config.report.risk_percent,
        stop_distance=stop_distance,
        rules=rules,
        deals=deals,
    )
