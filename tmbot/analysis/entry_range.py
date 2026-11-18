"""The acceptable entry range, and what each entry in it earns or loses.

A plan has one reference price, but the fill you get is rarely exactly it.  This
works out how far from the reference an entry can be before it breaks a rule the
bot already enforces -- it adds no new rule:

* WORST entry (the adverse end): the price where either
    - the loss at the stop, with the lots the plan advised, reaches the risk
      limit (``risk_percent`` of equity, costs included), or
    - TP1 no longer pays ``analysis.min_reward_risk`` times the risk.
  Whichever comes first is the limit.  Past it the same lots would lose more
  than allowed, or the trade would no longer pay for its risk.
* BEST entry (the favourable end): the price where the stop would be closer than
  ``analysis.min_stop_atr`` ATRs -- the stop's own minimum distance.
* PREFERRED entry: the plan's reference price, which is the price the lots were
  sized on.

The lots are never changed: a better entry only means a smaller loss at the same
stop, a worse one a bigger loss, up to the limit.  Stop and targets are the
plan's own and never move.

The range stops being valid (``check``) when the market or the conditions move:
price outside the range, TP1 already passed, the stop touched, a wider spread
pushing the loss over the limit, the plan older than its refresh interval, or a
newer plan that points the other way.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

SAMPLES = 5                  # entries shown across the range, best to worst
DEFAULT_VALID_HOURS = 4.0    # when report.intraday_refresh_hours is 0 (refresh off)


def _distance(entry: float, stop: float, cost: float) -> float:
    """Price at risk from ``entry`` to ``stop``, costs included."""
    return abs(entry - stop) + cost


def reward_risk(entry: float, target: float, stop: float, cost: float) -> float:
    """Reward over risk for one target at ``entry`` (costs come off the reward)."""
    risk = _distance(entry, stop, cost)
    reward = max(0.0, abs(target - entry) - cost)
    return reward / risk if risk > 0 else 0.0


def build(
    *,
    direction_sign: int,
    reference: float,
    stop: float,
    targets: Sequence[float],
    atr: float,
    spread: float,
    slippage: float,
    commission_per_lot: float,
    value_per_point: Optional[float],
    sizing: Optional[Dict[str, Any]],
    deal_targets: Sequence[int],
    min_reward_risk: float,
    min_stop_atr: float,
    created_at: datetime,
    valid_hours: float,
    bias: str,
) -> Dict[str, Any]:
    """The range for a plan, as a plain dict that is stored with the plan."""
    sign = direction_sign
    cost = max(0.0, spread) / 2.0 + max(0.0, slippage)
    sizing = sizing or {}
    per_leg = float(sizing.get("per_leg") or 0.0)
    legs = int(sizing.get("legs") or 0)
    volume = per_leg * legs
    max_money = float(sizing.get("max_money") or 0.0)
    have_lots = bool(volume > 0 and value_per_point and max_money > 0)
    value = float(value_per_point or 0.0)
    tp1 = targets[0]

    # ---- the worst entry: the first rule it would break
    worst = None
    limits: List[Tuple[float, str]] = []
    if have_lots:
        spare = (max_money - commission_per_lot * volume) / (value * volume) - cost
        if spare > 0:
            limits.append((stop + sign * spare, "risk"))
    m = max(0.0, min_reward_risk)
    if sign > 0:
        limits.append(((tp1 - cost + m * (stop - cost)) / (1 + m), "reward_risk"))
        # the adverse end of a long is the HIGHEST acceptable price
        worst, why = min(limits, key=lambda item: item[0]) if limits else (None, "")
    else:
        limits.append(((tp1 + cost + m * (stop + cost)) / (1 + m), "reward_risk"))
        worst, why = max(limits, key=lambda item: item[0]) if limits else (None, "")

    # ---- the best entry: the stop's own minimum distance
    best = stop + sign * max(0.0, min_stop_atr) * atr + sign * cost if atr > 0 else reference

    out: Dict[str, Any] = {
        "direction_sign": sign, "reference": reference, "stop": stop, "slippage": max(0.0, slippage),
        "targets": list(targets), "atr": atr, "cost": cost, "bias": bias,
        "created_at": created_at.isoformat(), "valid_hours": valid_hours,
        "min_reward_risk": min_reward_risk, "have_lots": have_lots,
        "per_leg": per_leg, "legs": legs, "max_money": max_money,
        "commission_per_lot": commission_per_lot, "value": value,
        "deal_targets": list(deal_targets), "currency": sizing.get("currency", ""),
        "equity": _equity_from(sizing), "limited_by": why,
    }

    # the reference must itself be acceptable, or there is nothing to offer
    acceptable = worst is not None and (reference <= worst if sign > 0 else reference >= worst)
    if not acceptable:
        out.update(valid=False, reason="no_acceptable_entry", low=None, high=None,
                   preferred=None, rows=[])
        return out

    # clamp the favourable end so it never crosses the reference
    best = min(best, reference) if sign > 0 else max(best, reference)
    low, high = (best, worst) if sign > 0 else (worst, best)
    out.update(valid=True, reason="", low=low, high=high, preferred=reference)
    out["rows"] = rows_for(out, _sample_prices(best, worst, reference))
    return out


def _equity_from(sizing: Dict[str, Any]) -> float:
    percent = float(sizing.get("risk_percent") or 0.0)
    money = float(sizing.get("max_money") or 0.0)
    return money / (percent / 100.0) if percent > 0 and money > 0 else 0.0


def _sample_prices(best: float, worst: float, reference: float) -> List[float]:
    """Evenly spaced entries from best to worst, plus the reference itself."""
    step = (worst - best) / (SAMPLES - 1)
    prices = [best + step * i for i in range(SAMPLES)] + [reference]
    unique: List[float] = []
    for price in sorted(prices, reverse=worst < best):
        if not unique or abs(price - unique[-1]) > 1e-9 * max(1.0, abs(price)):
            unique.append(price)
    return unique


def rows_for(r: Dict[str, Any], prices: Sequence[float]) -> List[Dict[str, Any]]:
    """Loss, profit and reward/risk at each entry, with the lots the plan advised."""
    stop, targets, cost = r["stop"], r["targets"], r["cost"]
    rows: List[Dict[str, Any]] = []
    for price in prices:
        row: Dict[str, Any] = {
            "entry": price,
            "risk_distance": round(_distance(price, stop, cost), 8),
            "rr": [round(reward_risk(price, tp, stop, cost), 3) for tp in targets],
            "is_preferred": abs(price - r["reference"]) < 1e-9 * max(1.0, abs(price)),
        }
        if r["have_lots"]:
            per_leg, legs, value = r["per_leg"], r["legs"], r["value"]
            commission = r["commission_per_lot"]
            loss = _distance(price, stop, cost) * value * per_leg * legs + commission * per_leg * legs
            profits = []
            for number in r["deal_targets"]:
                move = max(0.0, abs(targets[number - 1] - price) - cost)
                profits.append(round(move * value * per_leg - commission * per_leg, 2))
            row["loss"] = round(loss, 2)
            row["profits"] = profits
            row["profit_total"] = round(sum(profits), 2)
            row["rr_basket"] = round(sum(profits) / loss, 3) if loss > 0 else None
            row["within_limit"] = loss <= r["max_money"] + 1e-9
            row["risk_percent"] = (round(loss / r["equity"] * 100.0, 3)
                                   if r["equity"] else None)
        rows.append(row)
    return rows


def check(
    r: Dict[str, Any],
    *,
    price: float,
    spread: float,
    now: datetime,
    latest_bias: Optional[str] = None,
) -> Tuple[bool, List[Tuple[str, Dict[str, Any]]]]:
    """Is the range still valid?  Returns (valid, [(reason key, args), ...]).

    ``price`` is the price an entry would be made at right now.  Every reason is
    a rule from the module docstring, recomputed with the current spread.
    """
    if not r or not r.get("valid"):
        return False, [("no_acceptable_entry", {})]
    sign, stop, tp1 = r["direction_sign"], r["stop"], r["targets"][0]
    reasons: List[Tuple[str, Dict[str, Any]]] = []

    if (price - stop) * sign <= 0:
        reasons.append(("stop_touched", {"price": price, "stop": stop}))
    if (price - tp1) * sign >= 0:
        reasons.append(("tp1_passed", {"price": price, "tp1": tp1}))
    low, high = r["low"], r["high"]
    if price > high + 1e-9 or price < low - 1e-9:
        reasons.append(("outside_range", {"price": price, "low": low, "high": high}))

    # The costs now, not the costs when the plan was made.
    cost_now = max(0.0, spread) / 2.0 + r.get("slippage", 0.0)
    if r["have_lots"] and (price - stop) * sign > 0:
        loss = (_distance(price, stop, cost_now) * r["value"] * r["per_leg"] * r["legs"]
                + r["commission_per_lot"] * r["per_leg"] * r["legs"])
        if loss > r["max_money"] + 1e-9:
            reasons.append(("risk_over_limit", {"loss": round(loss, 2),
                                                "limit": round(r["max_money"], 2)}))
    if (price - stop) * sign > 0 and (price - tp1) * sign < 0 and (
            reward_risk(price, tp1, stop, cost_now) < r["min_reward_risk"] - 1e-9):
        reasons.append(("reward_risk_low", {
            "rr": round(reward_risk(price, tp1, stop, cost_now), 2),
            "minimum": r["min_reward_risk"]}))

    created = datetime.fromisoformat(r["created_at"])
    hours = r["valid_hours"] or DEFAULT_VALID_HOURS
    if now - created > timedelta(hours=hours):
        reasons.append(("expired", {"hours": f"{hours:g}"}))
    if latest_bias and r.get("bias") and latest_bias != r["bias"]:
        reasons.append(("bias_changed", {"old": r["bias"], "new": latest_bias}))
    return (not reasons), reasons

