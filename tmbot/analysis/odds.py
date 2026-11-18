"""Historical test of how often a target is reached before the stop.

No constants stand in for a probability here.  Every figure is a frequency
measured on the instrument's own candles:

    Take every bar of the history as a hypothetical entry (in the trade's
    direction, at its close).  Put a stop ``stop_atr`` ATRs away and each target
    ``d`` ATRs away, in *that bar's* ATR, so quiet and volatile periods are
    comparable.  Follow the price forward for ``horizon`` bars and record which
    came first, the target or the stop.  A bar that touches both counts as the
    stop (the pessimistic reading).  The share of test trades that reached the
    target first is the historical reach rate; the share that hit the stop
    first is the historical reversal-before-target rate.

Honest limits, which the numbers carry with them rather than hide:

* it is a frequency on past data for the same *geometry* (distances in ATR), not
  a forecast of this particular trade;
* test trades overlap in time, so the sample is smaller than its count suggests;
  the interval is a Wilson 95 % interval on the count and is therefore a little
  optimistic;
* when the trend at the test entry agrees with the trade's direction (EMA20 vs
  EMA50), only those test trades are used if there are enough of them, otherwise
  all are, and the result says which;
* below ``min_samples`` test trades no number is given at all -- ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt
from typing import Any, Dict, List, Optional, Sequence

from ..models import Candle, Direction
from .indicators import atr as atr_series
from .indicators import ema

WARMUP = 60            # bars before the first test entry (indicators need history)
Z95 = 1.96


def wilson(hits: int, n: int, z: float = Z95) -> Optional[tuple]:
    """Wilson score interval for ``hits`` of ``n``; None when n is 0."""
    if n <= 0:
        return None
    p = hits / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Reach:
    """What happened to the test trades for one target."""

    stage: str
    distance_atr: float
    samples: int
    hits: int            # target first
    stops: int           # stop first (a reversal before the target)
    neither: int         # neither within the horizon
    enough: bool         # samples >= min_samples

    @property
    def p(self) -> Optional[float]:
        return self.hits / self.samples if self.enough and self.samples else None

    @property
    def q(self) -> Optional[float]:
        return self.stops / self.samples if self.enough and self.samples else None

    @property
    def interval(self) -> Optional[tuple]:
        return wilson(self.hits, self.samples) if self.enough else None

    def to_dict(self) -> Dict[str, Any]:
        low_high = self.interval
        return {
            "stage": self.stage, "distance_atr": round(self.distance_atr, 3),
            "samples": self.samples, "hits": self.hits, "stops": self.stops,
            "neither": self.neither, "enough": self.enough,
            "p": None if self.p is None else round(self.p, 4),
            "q": None if self.q is None else round(self.q, 4),
            "low": None if low_high is None else round(low_high[0], 4),
            "high": None if low_high is None else round(low_high[1], 4),
        }


@dataclass
class OddsResult:
    reach: Dict[str, Reach] = field(default_factory=dict)
    samples: int = 0
    conditioned: bool = False        # only entries trending the trade's way
    bars: int = 0                    # candles of history used
    horizon: int = 0
    stop_atr: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "samples": self.samples, "conditioned": self.conditioned, "bars": self.bars,
            "horizon": self.horizon, "stop_atr": round(self.stop_atr, 3),
            "reach": {stage: r.to_dict() for stage, r in self.reach.items()},
        }


def reach_rates(
    candles: Sequence[Candle],
    direction: Direction,
    stop_atr: float,
    targets_atr: Dict[str, float],
    *,
    horizon: int,
    min_samples: int,
    atr_period: int = 14,
    trend_filter: bool = True,
) -> OddsResult:
    """Test ``stop_atr`` / ``targets_atr`` (in ATRs) over ``candles``.

    ``targets_atr`` maps a name ("TP1") to its distance in ATRs.  Distances that
    are not positive are skipped.  Returns an OddsResult whose ``Reach`` entries
    have ``enough == False`` (and ``p is None``) when the history is too short.
    """
    total = len(candles)
    result = OddsResult(bars=total, horizon=horizon, stop_atr=stop_atr)
    wanted = {name: d for name, d in targets_atr.items() if d and d > 0}
    if stop_atr <= 0 or horizon < 1 or not wanted:
        return result

    atrs = atr_series(candles, atr_period)
    closes = [c.close for c in candles]
    fast, slow = ema(closes, 20), ema(closes, 50)
    sign = direction.sign
    last_entry = total - 1 - horizon          # the whole horizon must be observed
    entries: List[int] = []
    aligned: List[int] = []
    for i in range(max(WARMUP, atr_period + 1), last_entry + 1):
        a = atrs[i]
        if a is None or a <= 0:
            continue
        entries.append(i)
        f, s = fast[i], slow[i]
        if f is not None and s is not None and (f - s) * sign > 0:
            aligned.append(i)

    use_trend = trend_filter and len(aligned) >= min_samples
    chosen = aligned if use_trend else entries
    result.conditioned = use_trend
    result.samples = len(chosen)

    tallies = {name: [0, 0, 0] for name in wanted}      # hits, stops, neither
    for i in chosen:
        a = atrs[i]
        entry = closes[i]
        stop = entry - sign * stop_atr * a
        levels = {name: entry + sign * d * a for name, d in wanted.items()}
        pending = set(wanted)
        for j in range(i + 1, i + 1 + horizon):
            bar = candles[j]
            stopped = bar.low <= stop if sign > 0 else bar.high >= stop
            for name in list(pending):
                level = levels[name]
                reached = bar.high >= level if sign > 0 else bar.low <= level
                if stopped:                       # the stop wins any tie
                    tallies[name][1] += 1
                    pending.discard(name)
                elif reached:
                    tallies[name][0] += 1
                    pending.discard(name)
            if not pending:
                break
        for name in pending:
            tallies[name][2] += 1

    for name, distance in wanted.items():
        hits, stops, neither = tallies[name]
        result.reach[name] = Reach(
            stage=name, distance_atr=distance, samples=result.samples, hits=hits,
            stops=stops, neither=neither, enough=result.samples >= min_samples,
        )
    return result
