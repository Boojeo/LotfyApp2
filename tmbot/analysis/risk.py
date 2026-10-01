"""The hard risk limit: the whole trade may lose at most N% of equity at its stop.

The stop is chosen by the analysis (structure, ATR, levels). This module never
moves it. It only decides how much size that stop can carry:

    allowed money   = equity x risk %
    loss per lot    = (stop distance + half the spread) x value of 1.0 per lot
    ideal volume    = allowed money / loss per lot        (for ALL legs together)

The limit applies to the whole basket, never per leg: three legs at 1% each
would be 3%. Each leg's volume is rounded DOWN to the broker's lot step, so
rounding can only ever reduce risk.

When the broker's minimum lot makes the planned legs exceed the limit, fewer
legs are advised (3 -> 2 -> 1). If even one leg at the minimum lot is over
the limit, the verdict is REJECTED. Risk is never rounded up to make a trade
fit -- a strong signal does not change that.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from ..models import MarketRules

OK = "OK"
REDUCED_LEGS = "REDUCED_LEGS"
REJECTED = "REJECTED"


@dataclass
class RiskAssessment:
    verdict: str                 # OK | REDUCED_LEGS | REJECTED
    equity: float
    currency: str
    risk_percent: float
    max_money: float             # the most the whole trade may lose at SL
    stop_distance: float         # entry to stop, price units, spread included
    loss_per_lot: float          # money lost at SL by 1.0 lot
    ideal_volume: float          # total volume that loses exactly max_money
    broker_min: float
    broker_step: float
    broker_max: Optional[float]
    legs_planned: int
    legs: int                    # legs advised (0 when rejected)
    per_leg: float               # volume per leg advised (0 when rejected)
    selected_volume: float       # legs x per_leg
    total_money: float           # loss at SL with the advised size
    total_percent: float
    within_limit: bool
    min_lot_money: float         # what one leg at the broker minimum loses
    min_lot_percent: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _floor_to_step(volume: float, step: float) -> float:
    steps = math.floor(round(volume / step, 9))
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(steps * step, decimals)


def assess(
    *,
    equity: float,
    currency: str,
    risk_percent: float,
    stop_distance: float,
    rules: MarketRules,
    legs_planned: int,
    spread: float = 0.0,
) -> Optional[RiskAssessment]:
    """Size the trade inside the limit, or say plainly that it cannot be.

    None when the inputs to judge it are missing (no equity, no contract
    value from the broker, no stop) -- unknown is not the same as safe, and
    the report says the check could not be made.
    """
    value = rules.value_per_point
    if not (equity and equity > 0 and risk_percent > 0 and stop_distance > 0 and value):
        return None

    legs_planned = max(1, legs_planned)
    # A long is bought at the ask and stopped at the bid: the spread is part
    # of the loss, so half of it is added to the distance from the mid.
    distance = stop_distance + max(0.0, spread) / 2.0
    max_money = equity * risk_percent / 100.0
    loss_per_lot = distance * value
    ideal = max_money / loss_per_lot

    step = rules.size_step or rules.min_deal_size or 0.01
    minimum = rules.min_deal_size or step
    maximum = rules.max_deal_size

    legs, per_leg = 0, 0.0
    for count in range(legs_planned, 0, -1):
        candidate = _floor_to_step(ideal / count, step)
        if maximum:
            candidate = min(candidate, _floor_to_step(maximum, step))
        if candidate >= minimum:
            legs, per_leg = count, candidate
            break

    selected = round(legs * per_leg, 8)
    total_money = selected * loss_per_lot
    min_lot_money = minimum * loss_per_lot
    if legs == 0:
        verdict = REJECTED
    elif legs < legs_planned:
        verdict = REDUCED_LEGS
    else:
        verdict = OK
    return RiskAssessment(
        verdict=verdict,
        equity=round(equity, 2),
        currency=currency,
        risk_percent=risk_percent,
        max_money=round(max_money, 2),
        stop_distance=distance,
        loss_per_lot=round(loss_per_lot, 4),
        ideal_volume=round(ideal, 6),
        broker_min=minimum,
        broker_step=step,
        broker_max=maximum,
        legs_planned=legs_planned,
        legs=legs,
        per_leg=per_leg,
        selected_volume=selected,
        total_money=round(total_money, 2),
        total_percent=round(total_money / equity * 100.0, 3),
        within_limit=legs > 0 and total_money <= max_money + 1e-9,
        min_lot_money=round(min_lot_money, 2),
        min_lot_percent=round(min_lot_money / equity * 100.0, 3),
    )


def basket_risk(
    *, volumes_and_distances: list, value_per_point: Optional[float], equity: float
) -> Optional[Dict[str, float]]:
    """What deals you actually opened lose at their stops, all together."""
    if not value_per_point or not equity:
        return None
    money = sum(volume * distance for volume, distance in volumes_and_distances) * value_per_point
    return {"money": round(money, 2), "percent": round(money / equity * 100.0, 3)}
