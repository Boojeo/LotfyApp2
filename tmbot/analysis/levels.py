"""Support/resistance and liquidity mapping, turned into three targets and a stop.

The idea is deliberately simple and inspectable: find confirmed swing points,
cluster them into zones, score the zones by how much attention price has paid
them, then walk outward from the current price picking the next three zones as
TP1/TP2/TP3 with a stop behind the nearest opposing zone.  When structure is
too thin to supply three usable targets, ATR projections fill the gaps so the
plan always contains exactly three targets and one stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ..config import AnalysisConfig
from ..models import Candle, Direction, Level
from .indicators import atr, last_value, swing_points


@dataclass
class LevelPlan:
    tp1: float
    tp2: float
    tp3: float
    sl: float
    atr: float
    levels: List[Level] = field(default_factory=list)
    method: str = "structure"
    notes: List[str] = field(default_factory=list)

    @property
    def targets(self) -> List[float]:
        return [self.tp1, self.tp2, self.tp3]


def _round_number_bonus(price: float, atr_value: float) -> float:
    """Reward zones that sit on a round number -- where resting orders pile up."""
    if atr_value <= 0:
        return 0.0
    for step, bonus in ((100.0, 0.6), (50.0, 0.4), (10.0, 0.25), (1.0, 0.1)):
        if step < atr_value / 4:
            continue
        if abs(price - round(price / step) * step) <= atr_value * 0.08:
            return bonus
    return 0.0


def find_zones(
    candles: Sequence[Candle],
    atr_value: float,
    config: AnalysisConfig,
    *,
    left: int = 2,
    right: int = 2,
) -> List[Level]:
    """Cluster confirmed swing points into scored zones."""
    if not candles or atr_value <= 0:
        return []

    highs, lows = swing_points(candles, left=left, right=right)
    tolerance = config.zone_tolerance_atr * atr_value
    liquidity_tolerance = max(tolerance * 0.3, atr_value * 0.12)
    total = len(candles)
    zones: List[Level] = []

    for points, kind in ((highs, "resistance"), (lows, "support")):
        if not points:
            continue
        ordered = sorted(points, key=lambda item: item[1])
        cluster: List[tuple[int, float]] = [ordered[0]]
        for point in ordered[1:]:
            if abs(point[1] - cluster[-1][1]) <= tolerance:
                cluster.append(point)
            else:
                zones.append(_zone_from_cluster(cluster, kind, total, atr_value, liquidity_tolerance))
                cluster = [point]
        zones.append(_zone_from_cluster(cluster, kind, total, atr_value, liquidity_tolerance))

    zones.sort(key=lambda zone: zone.price)
    return zones


def _zone_from_cluster(
    cluster: List[tuple[int, float]],
    kind: str,
    total_bars: int,
    atr_value: float,
    liquidity_tolerance: float,
) -> Level:
    prices = [price for _, price in cluster]
    indexes = [index for index, _ in cluster]
    price = sum(prices) / len(prices)
    touches = len(cluster)

    # Recency: a zone tested last week matters more than one from months ago.
    newest = max(indexes)
    recency = newest / total_bars if total_bars else 0.0

    # Equal highs/lows inside a tight band are a stop pool: price tends to be
    # drawn there before reversing, which makes them high-quality targets.
    spread = max(prices) - min(prices)
    is_liquidity = touches >= 2 and spread <= liquidity_tolerance

    score = touches + recency * 1.5 + (1.0 if is_liquidity else 0.0)
    score += _round_number_bonus(price, atr_value)

    label = "liquidity" if is_liquidity else kind
    return Level(
        price=price,
        kind=kind,
        score=score,
        touches=touches,
        is_liquidity=is_liquidity,
        label=f"{label} x{touches}",
    )


def _classify(zones: Sequence[Level], price: float) -> tuple[List[Level], List[Level]]:
    above = sorted((z for z in zones if z.price > price), key=lambda z: z.price)
    below = sorted((z for z in zones if z.price < price), key=lambda z: z.price, reverse=True)
    # A zone's role depends on which side of price it now sits, so relabel it
    # here rather than trusting the swing type it was built from.
    for zone, kind in [(z, "resistance") for z in above] + [(z, "support") for z in below]:
        zone.kind = kind
        zone.label = f"{'liquidity' if zone.is_liquidity else kind} x{zone.touches}"
    return above, below


def _pick_targets(
    candidates: Sequence[Level],
    price: float,
    atr_value: float,
    direction: Direction,
    config: AnalysisConfig,
) -> List[float]:
    """Walk outward taking zones that are far enough apart to be distinct targets."""
    minimum_first = config.min_first_target_atr * atr_value
    separation = config.min_target_separation_atr * atr_value
    chosen: List[float] = []
    for zone in candidates:
        distance = abs(zone.price - price)
        if distance < minimum_first:
            continue
        if chosen and abs(zone.price - chosen[-1]) < separation:
            continue
        chosen.append(zone.price)
        if len(chosen) == 3:
            break
    return chosen


def _fill_with_projections(
    targets: List[float],
    price: float,
    atr_value: float,
    direction: Direction,
    config: AnalysisConfig,
) -> tuple[List[float], bool]:
    """Top the list up to three using ATR multiples of the reference price."""
    used_projection = False
    separation = config.min_target_separation_atr * atr_value
    for index in range(len(targets), 3):
        projected = price + direction.sign * config.fallback_target_atr[index] * atr_value
        if targets:
            floor = targets[-1] + direction.sign * separation
            projected = max(projected, floor) if direction is Direction.BUY else min(projected, floor)
        targets.append(projected)
        used_projection = True
    return targets, used_projection


def _pick_stop(
    opposing: Sequence[Level],
    price: float,
    atr_value: float,
    direction: Direction,
    config: AnalysisConfig,
) -> tuple[float, str]:
    """Stop goes behind the nearest opposing zone, inside sane ATR bounds."""
    buffer = config.zone_tolerance_atr * 0.5 * atr_value
    minimum = config.min_stop_atr * atr_value
    maximum = config.max_stop_atr * atr_value
    for zone in opposing:
        stop = zone.price - direction.sign * buffer
        distance = abs(price - stop)
        if minimum <= distance <= maximum:
            return stop, f"behind {zone.label} @ {zone.price:.4f}"
    return price - direction.sign * config.fallback_stop_atr * atr_value, "ATR fallback"


def build_plan(
    candles: Sequence[Candle],
    price: float,
    direction: Direction,
    config: AnalysisConfig,
    *,
    atr_value: Optional[float] = None,
    extra_candles: Optional[Sequence[Candle]] = None,
) -> LevelPlan:
    """Produce exactly three targets and one stop for ``direction`` at ``price``."""
    resolved_atr = atr_value if atr_value is not None else last_value(atr(candles, config.atr_period))
    if not resolved_atr or resolved_atr <= 0:
        # Degenerate data (flat series): fall back to a fraction of price so the
        # plan is still structurally valid rather than a division by zero.
        resolved_atr = max(abs(price) * 0.002, 1e-6)

    zones = list(find_zones(candles, resolved_atr, config))
    if extra_candles:
        zones.extend(find_zones(extra_candles, resolved_atr, config))
        zones.sort(key=lambda zone: zone.price)

    above, below = _classify(zones, price)
    forward = above if direction is Direction.BUY else below
    opposing = below if direction is Direction.BUY else above

    targets = _pick_targets(forward, price, resolved_atr, direction, config)
    structural_count = len(targets)
    targets, used_projection = _fill_with_projections(
        targets, price, resolved_atr, direction, config
    )
    stop, stop_note = _pick_stop(opposing, price, resolved_atr, direction, config)

    # Guarantee the first target actually pays for the risk being taken.
    risk = abs(price - stop)
    notes: List[str] = [f"stop {stop_note}"]
    minimum_tp1 = price + direction.sign * config.min_reward_risk * risk
    if direction.is_beyond(minimum_tp1, targets[0]) and minimum_tp1 != targets[0]:
        notes.append(
            f"TP1 pushed to {config.min_reward_risk:.2f}R "
            f"({targets[0]:.4f} -> {minimum_tp1:.4f})"
        )
        targets[0] = minimum_tp1

    # Re-assert ordering and spacing after any adjustment.
    separation = config.min_target_separation_atr * resolved_atr
    for index in range(1, 3):
        floor = targets[index - 1] + direction.sign * separation
        if direction.is_beyond(floor, targets[index]):
            targets[index] = floor

    method = "structure" if structural_count == 3 else (
        "hybrid" if structural_count else "atr-projection"
    )
    if used_projection and structural_count:
        notes.append(f"{structural_count} structural target(s), rest projected from ATR")
    elif not structural_count:
        notes.append("no qualifying structure above/below price; targets projected from ATR")

    return LevelPlan(
        tp1=targets[0],
        tp2=targets[1],
        tp3=targets[2],
        sl=stop,
        atr=resolved_atr,
        levels=sorted(zones, key=lambda zone: zone.score, reverse=True)[:12],
        method=method,
        notes=notes,
    )
