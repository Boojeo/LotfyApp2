"""Market map: price gaps, Fibonacci, and every family of support/resistance.

Everything here is deterministic and reads candles only.  It *describes* the
chart -- it never moves the targets or the stop, which stay with ``levels``.

* Fair value gaps (three-candle price gaps), typed and tracked until filled.
* Fibonacci retracements/extensions on the latest confirmed swing leg, with a
  plain reading of where price sits in it.
* Static support/resistance (clustered swing zones, from ``levels``).
* Dynamic support/resistance (moving averages that price leans on).
* Wolfe waves (five-point converging wedge) with the projected target.
* Camarilla pivots from the previous session.

The result is a plain dict so it is stored with the plan and survives a restart.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import AnalysisConfig
from ..models import Candle
from .indicators import ema, last_value, swing_points
from .levels import find_zones

GAP_LOOKBACK = 120          # bars scanned for gaps
GAP_MIN_ATR = 0.1           # smaller than this is noise, not a gap
GAP_COMMON_ATR = 0.3        # below this the gap is "common"
GAP_RANGE_BARS = 20         # the range a breakaway gap must have left
GAP_TIGHT_RANGE_ATR = 4.0   # ...and that range must have been this tight
GAP_EXHAUSTION_ATR = 2.5    # already this far from the EMA20: late in the move
GAP_KEEP = 6

FIB_RETRACEMENTS = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_EXTENSIONS = (1.272, 1.618)
FIB_MIN_LEG_ATR = 1.5
FIB_SWING = 3
FIB_LEGS = 3                # most recent legs considered

WOLFE_SWING = 3
WOLFE_LOOKBACK = 160
WOLFE_FRESH_BARS = 25       # point 5 must be this recent

CAMARILLA_FACTORS = (1.1 / 12, 1.1 / 6, 1.1 / 4, 1.1 / 2)   # R1..R4 / S1..S4


def analyse(
    entry: Sequence[Candle],
    structure: Sequence[Candle],
    daily: Sequence[Candle],
    price: float,
    atr_value: float,
    config: AnalysisConfig,
) -> Dict[str, Any]:
    """The whole map at ``price``.  Each part is independent and may be empty."""
    if not entry or atr_value <= 0:
        return {}
    swing_source = structure if len(structure) >= 60 else entry
    return {
        "gaps": fair_value_gaps(entry, price, atr_value),
        "fibonacci": fibonacci(swing_source, price, atr_value),
        "static": static_levels(entry, structure, price, atr_value, config),
        "dynamic": dynamic_levels(entry, price),
        "wolfe": wolfe_wave(entry, price),
        "camarilla": camarilla(daily, price),
    }


# ------------------------------------------------------------------ fair value gaps

def fair_value_gaps(
    candles: Sequence[Candle], price: float, atr_value: float
) -> List[Dict[str, Any]]:
    """Open three-candle gaps, nearest to ``price`` first.

    A bullish gap is candle 1's high below candle 3's low (price moved up too
    fast to trade in between); bearish is the mirror.  A gap is *filled* once
    price trades back through all of it, and only unfilled ones are returned.
    """
    total = len(candles)
    if total < 3:
        return []
    fast = ema([c.close for c in candles], 20)
    slow = ema([c.close for c in candles], 50)
    found: List[Dict[str, Any]] = []
    for index in range(max(2, total - GAP_LOOKBACK), total):
        first, third = candles[index - 2], candles[index]
        if first.high < third.low:
            kind, low, high = "bullish", first.high, third.low
        elif first.low > third.high:
            kind, low, high = "bearish", third.high, first.low
        else:
            continue
        size = high - low
        if size < GAP_MIN_ATR * atr_value:
            continue
        later = candles[index + 1:]
        if kind == "bullish":
            depth = (high - min((c.low for c in later), default=high)) / size
        else:
            depth = (max((c.high for c in later), default=low) - low) / size
        if depth >= 1.0:
            continue
        found.append({
            "kind": kind,
            "type": _gap_type(candles, index, kind, size / atr_value, atr_value, fast, slow),
            "low": low,
            "high": high,
            "size_atr": round(size / atr_value, 2),
            "state": "partial" if depth > 0 else "open",
            "filled": round(max(depth, 0.0), 2),
            "bars_ago": total - 1 - index,
        })
    found.sort(key=lambda gap: _distance(gap["low"], gap["high"], price))
    return found[:GAP_KEEP]


def _gap_type(candles, index, kind, size_atr, atr_value, fast, slow) -> str:
    """breakaway | continuation | exhaustion | common."""
    if size_atr < GAP_COMMON_ATR:
        return "common"
    sign = 1 if kind == "bullish" else -1
    close = candles[index].close
    prior = candles[max(0, index - 2 - GAP_RANGE_BARS): index - 2]
    broke_out = bool(prior) and (
        close > max(c.high for c in prior) if sign > 0 else close < min(c.low for c in prior)
    )
    tight = bool(prior) and (
        max(c.high for c in prior) - min(c.low for c in prior)
    ) <= GAP_TIGHT_RANGE_ATR * atr_value
    # Trend and stretch are read just *before* the impulse: the gap itself
    # drags the averages and stretches price, which would make every breakout
    # look like exhaustion.
    before = index - 2
    f, s = fast[before], slow[before]
    with_trend = f is not None and s is not None and (f - s) * sign > 0
    stretch = (candles[before].close - f) * sign / atr_value if f is not None else 0.0
    if with_trend and stretch >= GAP_EXHAUSTION_ATR:
        return "exhaustion"
    if broke_out and tight:
        return "breakaway"
    if with_trend:
        return "continuation"
    return "breakaway" if broke_out else "common"


def _distance(low: float, high: float, price: float) -> float:
    return 0.0 if low <= price <= high else min(abs(price - low), abs(price - high))


# ------------------------------------------------------------------ fibonacci

def fibonacci(
    candles: Sequence[Candle], price: float, atr_value: float
) -> Optional[Dict[str, Any]]:
    """Retracement/extension levels of the latest confirmed swing leg."""
    # Measure the dominant leg among the latest swings, not just the latest
    # one: a confirmed pullback low would otherwise replace the very move
    # whose retracement we are trying to read.
    swings = _alternating_swings(candles, FIB_SWING)[-FIB_LEGS - 1:]
    legs = [(abs(b[2] - a[2]), a, b) for a, b in zip(swings, swings[1:])]
    legs = [leg for leg in legs if leg[0] >= FIB_MIN_LEG_ATR * atr_value]
    if not legs:
        return None
    leg, first, second = max(legs, key=lambda item: (item[0], item[2][1]))
    up = second[0] == "H"                # the leg ran low -> high
    start, end = first[2], second[2]

    def at(ratio: float) -> float:       # retracement: back toward ``start``
        return end - ratio * leg if up else end + ratio * leg

    def extension(ratio: float) -> float:  # continuation beyond ``end``
        return start + ratio * leg if up else start - ratio * leg

    depth = (end - price) / leg if up else (price - end) / leg
    levels = [{"ratio": r, "price": at(r), "kind": "retracement"} for r in FIB_RETRACEMENTS]
    levels += [{"ratio": r, "price": extension(r), "kind": "extension"} for r in FIB_EXTENSIONS]
    for level in levels:
        level["role"] = "support" if level["price"] < price else "resistance"
    return {
        "direction": "up" if up else "down",
        "start": start,
        "end": end,
        "depth": round(depth, 3),
        "zone": _fib_zone(depth),
        "levels": levels,
    }


def _fib_zone(depth: float) -> str:
    if depth < 0:
        return "extension"
    if depth < 0.382:
        return "shallow"
    if depth < 0.5:
        return "moderate"
    if depth < 0.618:
        return "deep"
    if depth <= 0.786:
        return "golden"
    if depth <= 1.0:
        return "very_deep"
    return "broken"


# ------------------------------------------------------------------ support / resistance

def static_levels(
    entry: Sequence[Candle],
    structure: Sequence[Candle],
    price: float,
    atr_value: float,
    config: AnalysisConfig,
) -> Dict[str, Any]:
    """Nearest clustered swing zone on each side of ``price``."""
    zones = list(find_zones(entry, atr_value, config))
    if structure:
        zones += find_zones(structure, atr_value, config)

    def nearest(candidates):
        if not candidates:
            return None
        zone = min(candidates, key=lambda z: abs(z.price - price))
        return {"price": zone.price, "touches": zone.touches,
                "liquidity": zone.is_liquidity}

    return {
        "support": nearest([z for z in zones if z.price < price]),
        "resistance": nearest([z for z in zones if z.price > price]),
    }


def dynamic_levels(candles: Sequence[Candle], price: float) -> List[Dict[str, Any]]:
    """Moving averages price leans on: below it they hold, above it they cap."""
    closes = [c.close for c in candles]
    out: List[Dict[str, Any]] = []
    for period in (20, 50, 200):
        if len(closes) < period + 5:
            continue
        series = ema(closes, period)
        value, earlier = last_value(series), series[-6]
        if value is None:
            continue
        out.append({
            "name": f"EMA{period}",
            "price": value,
            "role": "support" if value < price else "resistance",
            "rising": earlier is not None and value > earlier,
        })
    return out


# ------------------------------------------------------------------ wolfe wave

def _alternating_swings(
    candles: Sequence[Candle], size: int = WOLFE_SWING
) -> List[Tuple[str, int, float]]:
    highs, lows = swing_points(candles, left=size, right=size)
    points = sorted([("H", i, p) for i, p in highs] + [("L", i, p) for i, p in lows],
                    key=lambda point: point[1])
    merged: List[Tuple[str, int, float]] = []
    for point in points:
        if merged and merged[-1][0] == point[0]:
            keep_new = point[2] > merged[-1][2] if point[0] == "H" else point[2] < merged[-1][2]
            if keep_new:
                merged[-1] = point
        else:
            merged.append(point)
    return merged


def wolfe_wave(candles: Sequence[Candle], price: float) -> Optional[Dict[str, Any]]:
    """A converging five-point wedge and where it points.

    Bullish: lows 1, 3, 5 falling and highs 2, 4 falling, the 2-4 line closing
    on the 1-3 line, point 5 pushing through the 1-3 line.  The target is the
    1-4 line extended to the latest bar.  Bearish is the mirror image.
    """
    window = candles[-WOLFE_LOOKBACK:]
    offset = len(candles) - len(window)
    swings = _alternating_swings(window)
    if len(swings) < 5:
        return None
    five = swings[-5:]
    last = len(window) - 1
    if last - five[4][1] > WOLFE_FRESH_BARS:
        return None
    kinds = "".join(point[0] for point in five)
    if kinds not in ("LHLHL", "HLHLH"):
        return None
    bullish = kinds == "LHLHL"
    (_, i1, p1), (_, i2, p2), (_, i3, p3), (_, i4, p4), (_, i5, p5) = five

    def line(ia, pa, ib, pb, at):
        return pa + (pb - pa) * (at - ia) / (ib - ia)

    sign = 1 if bullish else -1
    # points 3 and 5 push further out than 1 and 3; 4 stays inside 2.
    if not (p3 * sign < p1 * sign and p5 * sign < p3 * sign and p4 * sign < p2 * sign):
        return None
    # the wedge narrows: gap between the 1-3 and 2-4 lines shrinks from 2 to 4.
    width2 = abs(p2 - line(i1, p1, i3, p3, i2))
    width4 = abs(p4 - line(i1, p1, i3, p3, i4))
    if not 0 < width4 < width2:
        return None
    # point 5 overshoots the 1-3 line.
    if p5 * sign >= line(i1, p1, i3, p3, i5) * sign:
        return None
    target = line(i1, p1, i4, p4, last)
    if (target - price) * sign <= 0:
        return None
    return {
        "direction": "bullish" if bullish else "bearish",
        "entry": p5,
        "target": target,
        "points": [(offset + i, p) for _, i, p in five],
        "bars_ago": last - i5,
    }


# ------------------------------------------------------------------ camarilla

def camarilla(daily: Sequence[Candle], price: float) -> Optional[Dict[str, Any]]:
    """Pivot levels from the previous completed session."""
    if len(daily) < 2:
        return None
    prev = daily[-2]
    spread = prev.high - prev.low
    if spread <= 0:
        return None
    levels: Dict[str, float] = {"pivot": (prev.high + prev.low + prev.close) / 3.0}
    for number, factor in enumerate(CAMARILLA_FACTORS, start=1):
        levels[f"r{number}"] = prev.close + spread * factor
        levels[f"s{number}"] = prev.close - spread * factor
    if price > levels["r4"]:
        zone = "above_r4"
    elif price > levels["r3"]:
        zone = "above_r3"
    elif price < levels["s4"]:
        zone = "below_s4"
    elif price < levels["s3"]:
        zone = "below_s3"
    else:
        zone = "range"
    return {**levels, "zone": zone}
