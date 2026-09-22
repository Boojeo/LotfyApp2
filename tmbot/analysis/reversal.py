"""Trend-reversal detection.

Deliberately deterministic.  A language model is seconds slow, gives a
different answer to the same input twice, and cannot be backtested -- none of
which belong on the path that decides whether to pull your stop in.  So the
detector is four independent, checkable signals, and the model's only job
elsewhere is to explain what the rules already decided.

Detection is also deliberately hard to trigger.  ``min_signals`` independent
signals must agree, *and* they must keep agreeing for ``confirm_cycles`` in a
row.  A single bar poking through a swing low is noise; that is what stops are
for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import ReversalConfig
from ..i18n import Translator
from ..models import Direction, ManagedTrade, MarketSnapshot, TrendStrength


@dataclass(frozen=True)
class Signal:
    """One piece of evidence.

    ``detail`` is English for the log; ``key``/``args`` render the same thing
    in the user's language. The evidence line is what the reader uses to
    decide, so leaving it English would defeat the point of translating at all.
    """

    name: str
    fired: bool
    detail: str
    key: str = ""
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReversalSignal:
    """The verdict for one evaluation of one trade."""

    detected: bool
    against: Direction              # the side the market now favours
    agreeing: int
    required: int
    confidence: float               # 0..100
    signals: List[Signal] = field(default_factory=list)
    confirmed: bool = False         # detected for long enough to act on
    streak: int = 0

    @property
    def fired(self) -> List[Signal]:
        return [signal for signal in self.signals if signal.fired]

    def summary(self, t: Optional["Translator"] = None) -> str:
        parts = [
            t(signal.key, **signal.args) if (t and signal.key) else signal.detail
            for signal in self.fired
        ]
        if not parts:
            return "-"
        return (t.join(parts) if t else ", ".join(parts))


def _crossed(
    fast_now: Optional[float],
    slow_now: Optional[float],
    fast_prev: Optional[float],
    slow_prev: Optional[float],
) -> bool:
    """True when ``fast`` has just crossed below ``slow``."""
    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return False
    return fast_prev >= slow_prev and fast_now < slow_now


def detect(
    trade: ManagedTrade,
    snapshot: MarketSnapshot,
    config: ReversalConfig,
) -> ReversalSignal:
    """Score the case that ``trade``'s direction has stopped working."""
    against = trade.direction.opposite
    signals: List[Signal] = []
    long = trade.direction is Direction.BUY

    # 1. Directional index cross -- the trend's own measure changing sides.
    if long:
        di_cross = _crossed(snapshot.plus_di, snapshot.minus_di,
                            snapshot.plus_di_prev, snapshot.minus_di_prev)
    else:
        di_cross = _crossed(snapshot.minus_di, snapshot.plus_di,
                            snapshot.minus_di_prev, snapshot.plus_di_prev)
    plus = f"{snapshot.plus_di:.1f}" if snapshot.plus_di is not None else "-"
    minus = f"{snapshot.minus_di:.1f}" if snapshot.minus_di is not None else "-"
    signals.append(Signal(
        "di_cross", bool(di_cross),
        f"{'-DI crossed +DI' if long else '+DI crossed -DI'} (+DI {plus} / -DI {minus})",
        key=f"signal.di_cross.{'buy' if long else 'sell'}",
        args={"plus": plus, "minus": minus},
    ))

    # 2. Structure break -- price closing beyond the last confirmed swing.
    close = snapshot.last_close
    level = snapshot.swing_low if long else snapshot.swing_high
    structure = bool(
        close is not None and level is not None
        and (close < level if long else close > level)
    )
    signals.append(Signal(
        "structure", structure,
        f"structure broke {level:g}" if level is not None else "structure break",
        key="signal.structure",
        args={"level": f"{level:g}" if level is not None else "-"},
    ))

    # 3. Moving-average cross.
    if long:
        ma_cross = _crossed(snapshot.ema_fast, snapshot.ema_slow,
                            snapshot.ema_fast_prev, snapshot.ema_slow_prev)
    else:
        ma_cross = _crossed(snapshot.ema_slow, snapshot.ema_fast,
                            snapshot.ema_slow_prev, snapshot.ema_fast_prev)
    signals.append(Signal(
        "ema_cross", bool(ma_cross), "EMA20 crossed EMA50", key="signal.ema_cross",
    ))

    # 4. Momentum flipping and widening, not merely flat.
    hist, hist_prev = snapshot.macd_hist, snapshot.macd_hist_prev
    momentum = bool(
        hist is not None and hist_prev is not None
        and (hist < 0 <= hist_prev if long else hist > 0 >= hist_prev)
        and abs(hist) > abs(hist_prev)
    )
    signals.append(Signal(
        "momentum", momentum,
        f"MACD flipped to {hist:+.4g}" if hist is not None else "momentum flip",
        key="signal.momentum",
        args={"value": f"{hist:+.4g}" if hist is not None else "-"},
    ))

    agreeing = sum(1 for signal in signals if signal.fired)

    # A market with no trend has no trend to reverse; ADX gates the whole thing.
    trending = snapshot.adx >= config.adx_min
    detected = trending and agreeing >= config.min_signals

    confidence = 0.0
    if detected:
        share = agreeing / len(signals)
        adx_weight = min(snapshot.adx, 45.0) / 45.0
        confidence = min(100.0, share * 65.0 + adx_weight * 35.0)

    streak = trade.reversal_streak + 1 if detected else 0
    return ReversalSignal(
        detected=detected,
        against=against,
        agreeing=agreeing,
        required=config.min_signals,
        confidence=confidence,
        signals=signals,
        confirmed=detected and streak >= config.confirm_cycles,
        streak=streak,
    )


def recovery_stop(
    trade: ManagedTrade,
    snapshot: MarketSnapshot,
    config: ReversalConfig,
) -> Optional[float]:
    """Where the stop should go on a confirmed reversal.

    Pulled to ``tighten_atr`` behind the current price -- close enough to
    salvage the move, far enough not to be clipped by the spread.  The caller
    still applies the ratchet and the broker's minimum-distance rule, so this
    can only ever tighten.
    """
    if snapshot.atr <= 0:
        return None
    price = snapshot.quote.exit_price(trade.direction)
    return price - trade.direction.sign * config.tighten_atr * snapshot.atr
