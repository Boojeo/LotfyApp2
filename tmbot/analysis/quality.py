"""Entry quality: a correct direction is not automatically a good entry.

The direction says which way the market leans. This module asks whether
*here and now* is a sensible place to join it, and says why not:

    Direction: BEARISH  |  Entry quality: WEAK  |  price stretched 2.4 ATR
    below its average and close to support

Higher and lower timeframes are not averaged into one score. The higher one
gives context: if it points the other way, that is a CONFLICT and lowers the
quality of the entry instead of being blended into an arbitrary number.

Finally the verdict combines direction, quality and the hard risk check.
The bot never opens trades, so the verdict is advice for the deal you place.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..models import Bias, Direction, Level

GOOD, WEAK, POOR, NONE = "GOOD", "WEAK", "POOR", "NONE"
ALIGNED, PARTIAL, CONFLICT, UNCERTAIN = "ALIGNED", "PARTIAL", "CONFLICT", "UNCERTAIN"
APPROVED, CAUTION, REJECTED = "APPROVED", "CAUTION", "REJECTED"

MAJOR, MINOR = "major", "minor"

# Thresholds, in ATRs of the entry chart unless noted.
EXTENDED_ATR = 2.0          # distance from EMA20 that counts as stretched
NEAR_LEVEL_ATR = 0.5        # an opposing level this close is in the way
RSI_STRETCHED = (30.0, 70.0)
WIDE_SPREAD_SHARE = 0.20    # spread above this share of the stop distance


@dataclass
class Flag:
    key: str
    severity: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EntryAssessment:
    quality: str                        # GOOD | WEAK | POOR | NONE
    alignment: str                      # ALIGNED | PARTIAL | CONFLICT | UNCERTAIN
    higher_timeframe: str
    higher_bias: str                    # BULLISH | BEARISH | NEUTRAL | ""
    flags: List[Flag] = field(default_factory=list)
    verdict: str = CAUTION              # APPROVED | CAUTION | REJECTED
    verdict_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def alignment(direction: Direction, higher: Optional[Bias]) -> str:
    if higher is None:
        return UNCERTAIN
    if higher is Bias.NEUTRAL:
        return PARTIAL
    return ALIGNED if higher.direction is direction else CONFLICT


def assess(
    *,
    bias: Bias,
    direction: Direction,
    price: float,
    atr: float,
    ema_fast: float,
    rsi: float,
    stop: float,
    tp1: float,
    levels: List[Level],
    spread: float,
    tp1_pushed: bool,
    higher_timeframe: str,
    higher_bias: Optional[Bias],
    risk_verdict: Optional[str],
) -> EntryAssessment:
    sign = direction.sign
    flags: List[Flag] = []
    aligned = alignment(direction, higher_bias)

    if bias.direction is not None and bias.direction is not direction:
        # Levels built for the side you are on, against the analysis.
        flags.append(Flag("against_bias", MAJOR, {"bias": bias.value}))

    if aligned == CONFLICT:
        flags.append(Flag("timeframe_conflict", MAJOR,
                          {"timeframe": higher_timeframe,
                           "bias": higher_bias.value if higher_bias else ""}))
    elif aligned == PARTIAL:
        flags.append(Flag("timeframe_partial", MINOR, {"timeframe": higher_timeframe}))

    if atr > 0:
        stretch = (price - ema_fast) * sign / atr
        if stretch > EXTENDED_ATR:
            flags.append(Flag("extended", MINOR, {"atr": f"{stretch:.1f}"}))

        # The nearest level standing between price and TP1, against the trade.
        blocking = [
            level.price for level in levels
            if (level.price - price) * sign > 0 and (tp1 - level.price) * sign > 0
        ]
        if blocking:
            nearest = min(blocking, key=lambda value: abs(value - price))
            gap = abs(nearest - price) / atr
            if gap < NEAR_LEVEL_ATR:
                flags.append(Flag("near_level", MINOR,
                                  {"level": nearest, "atr": f"{gap:.1f}"}))

    if (sign > 0 and rsi >= RSI_STRETCHED[1]) or (sign < 0 and rsi <= RSI_STRETCHED[0]):
        flags.append(Flag("rsi_stretched", MINOR, {"rsi": f"{rsi:.0f}"}))

    if tp1_pushed:
        flags.append(Flag("tp1_pushed", MINOR, {}))

    stop_distance = abs(price - stop)
    if stop_distance > 0 and spread / stop_distance > WIDE_SPREAD_SHARE:
        flags.append(Flag("wide_spread", MINOR,
                          {"share": f"{spread / stop_distance:.0%}"}))

    if risk_verdict == "REJECTED":
        flags.append(Flag("risk_limit", MAJOR, {}))

    if bias is Bias.NEUTRAL:
        quality = NONE
    elif any(flag.severity == MAJOR for flag in flags):
        quality = POOR
    else:
        minor = sum(1 for flag in flags if flag.severity == MINOR)
        quality = GOOD if minor == 0 else WEAK if minor < 3 else POOR

    result = EntryAssessment(
        quality=quality, alignment=aligned, higher_timeframe=higher_timeframe,
        higher_bias=higher_bias.value if higher_bias else "", flags=flags,
    )
    result.verdict, result.verdict_key = _verdict(bias, quality, risk_verdict)
    return result


def _verdict(bias: Bias, quality: str, risk_verdict: Optional[str]) -> tuple:
    """Risk first, then direction, then quality. Signal strength plays no part."""
    if risk_verdict == "REJECTED":
        return REJECTED, "verdict.risk_limit"
    if bias is Bias.NEUTRAL:
        return REJECTED, "verdict.no_direction"
    if quality == POOR:
        return REJECTED, "verdict.poor_entry"
    if risk_verdict in (None, "UNCHECKED"):
        return CAUTION, "verdict.risk_unchecked"
    if quality == WEAK:
        return CAUTION, "verdict.weak_entry"
    return APPROVED, "verdict.approved"
