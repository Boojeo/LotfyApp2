"""Pure-Python indicators (no numpy/pandas dependency).

Every function returns a list aligned with the input, padded with ``None``
until the indicator has enough history.  That alignment makes it safe to zip
indicator output against candles without off-by-one bookkeeping.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ..models import Candle

Series = List[Optional[float]]


def sma(values: Sequence[float], period: int) -> Series:
    if period <= 0:
        raise ValueError("period must be positive")
    out: Series = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            out[index] = running / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    if period <= 0:
        raise ValueError("period must be positive")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        out[index] = current
    return out


def wilder_smooth(values: Sequence[float], period: int) -> Series:
    """Wilder's smoothing, as used by RSI/ATR/ADX."""
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    for index in range(period, len(values)):
        current = (current * (period - 1) + values[index]) / period
        out[index] = current
    return out


def true_range(candles: Sequence[Candle]) -> List[float]:
    out: List[float] = []
    for index, candle in enumerate(candles):
        if index == 0:
            out.append(candle.high - candle.low)
            continue
        previous_close = candles[index - 1].close
        out.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> Series:
    return wilder_smooth(true_range(candles), period)


def rsi(values: Sequence[float], period: int = 14) -> Series:
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains: List[float] = [0.0]
    losses: List[float] = [0.0]
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = wilder_smooth(gains[1:], period)
    average_loss = wilder_smooth(losses[1:], period)
    for index in range(len(average_gain)):
        gain, loss = average_gain[index], average_loss[index]
        if gain is None or loss is None:
            continue
        if loss == 0:
            out[index + 1] = 100.0
        else:
            rs = gain / loss
            out[index + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out


def adx(candles: Sequence[Candle], period: int = 14) -> Tuple[Series, Series, Series]:
    """Return ``(adx, plus_di, minus_di)``."""
    length = len(candles)
    empty: Series = [None] * length
    if length < period * 2:
        return empty, list(empty), list(empty)

    plus_dm: List[float] = [0.0]
    minus_dm: List[float] = [0.0]
    for index in range(1, length):
        up = candles[index].high - candles[index - 1].high
        down = candles[index - 1].low - candles[index].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    tr = true_range(candles)
    smooth_tr = wilder_smooth(tr[1:], period)
    smooth_plus = wilder_smooth(plus_dm[1:], period)
    smooth_minus = wilder_smooth(minus_dm[1:], period)

    plus_di: Series = [None] * length
    minus_di: Series = [None] * length
    dx_values: List[float] = []
    dx_index: List[int] = []
    for index in range(len(smooth_tr)):
        tr_value = smooth_tr[index]
        if not tr_value:
            continue
        p = 100.0 * (smooth_plus[index] or 0.0) / tr_value
        m = 100.0 * (smooth_minus[index] or 0.0) / tr_value
        plus_di[index + 1] = p
        minus_di[index + 1] = m
        total = p + m
        dx_values.append(100.0 * abs(p - m) / total if total else 0.0)
        dx_index.append(index + 1)

    adx_series: Series = [None] * length
    smoothed_dx = wilder_smooth(dx_values, period)
    for position, value in enumerate(smoothed_dx):
        if value is not None:
            adx_series[dx_index[position]] = value
    return adx_series, plus_di, minus_di


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[Series, Series, Series]:
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    macd_line: Series = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(fast_line, slow_line)
    ]
    defined = [value for value in macd_line if value is not None]
    signal_values = ema(defined, signal)
    signal_line: Series = [None] * len(macd_line)
    offset = len(macd_line) - len(defined)
    for index, value in enumerate(signal_values):
        signal_line[offset + index] = value
    histogram: Series = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram


def swing_points(
    candles: Sequence[Candle], left: int = 2, right: int = 2
) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Confirmed fractal highs and lows.

    A point is only reported once ``right`` bars have closed beyond it, so a
    swing never gets revised away under the trailing logic.
    """
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    for index in range(left, len(candles) - right):
        window = candles[index - left: index + right + 1]
        candle = candles[index]
        if all(candle.high >= other.high for other in window) and any(
            candle.high > other.high for other in window
        ):
            highs.append((index, candle.high))
        if all(candle.low <= other.low for other in window) and any(
            candle.low < other.low for other in window
        ):
            lows.append((index, candle.low))
    return highs, lows


def last_value(series: Series) -> Optional[float]:
    for value in reversed(series):
        if value is not None:
            return value
    return None


def slope(series: Series, lookback: int = 10) -> Optional[float]:
    """Average per-bar change of the last ``lookback`` defined values."""
    defined = [value for value in series if value is not None]
    if len(defined) < 2:
        return None
    window = defined[-lookback:]
    if len(window) < 2:
        return None
    return (window[-1] - window[0]) / (len(window) - 1)
