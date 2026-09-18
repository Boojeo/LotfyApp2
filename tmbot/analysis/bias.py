"""Technical read: a signed score, a bias label and a trend-strength grade.

Every input is an explicit weighted factor so the daily report can show its
work -- a bias you cannot audit is a bias you cannot trust when it is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from ..config import AnalysisConfig, ManagementConfig
from ..models import Bias, Candle, TrendStrength
from .indicators import adx, atr, ema, last_value, macd, rsi, slope


@dataclass
class Factor:
    name: str
    value: float   # normalised to [-1, 1]
    weight: float
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 3),
            "weight": self.weight,
            "detail": self.detail,
        }


@dataclass
class TechnicalRead:
    bias: Bias
    score: float          # -100 (max bearish) .. +100 (max bullish)
    confidence: float     # 0 .. 100
    strength: TrendStrength
    price: float
    atr: float
    adx: float
    rsi: float
    ema_fast: float
    ema_slow: float
    ema_trend: float
    factors: List[Factor] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bias": self.bias.value,
            "score": round(self.score, 2),
            "confidence": round(self.confidence, 1),
            "strength": self.strength.value,
            "price": self.price,
            "atr": round(self.atr, 6),
            "adx": round(self.adx, 2),
            "rsi": round(self.rsi, 2),
            "ema_fast": round(self.ema_fast, 6),
            "ema_slow": round(self.ema_slow, 6),
            "ema_trend": round(self.ema_trend, 6),
            "factors": [factor.to_dict() for factor in self.factors],
        }


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def trend_strength(adx_value: float, config: ManagementConfig) -> TrendStrength:
    if adx_value >= config.adx_strong:
        return TrendStrength.STRONG
    if adx_value >= config.adx_moderate:
        return TrendStrength.MODERATE
    return TrendStrength.WEAK


def analyse(
    candles: Sequence[Candle],
    config: AnalysisConfig,
    *,
    management: ManagementConfig | None = None,
) -> TechnicalRead:
    """Score the technical picture from a single timeframe's candles."""
    if len(candles) < 60:
        raise ValueError(f"need at least 60 candles for a technical read, got {len(candles)}")

    management = management or ManagementConfig()
    closes = [candle.close for candle in candles]
    price = closes[-1]

    atr_value = last_value(atr(candles, config.atr_period)) or 0.0
    adx_series, plus_di, minus_di = adx(candles, 14)
    adx_value = last_value(adx_series) or 0.0
    di_plus = last_value(plus_di) or 0.0
    di_minus = last_value(minus_di) or 0.0
    rsi_value = last_value(rsi(closes, 14)) or 50.0
    ema20 = last_value(ema(closes, 20)) or price
    ema50 = last_value(ema(closes, 50)) or price
    ema200 = last_value(ema(closes, 200)) or ema50
    _, _, histogram = macd(closes)
    histogram_value = last_value(histogram) or 0.0
    ema20_slope = slope(ema(closes, 20), lookback=10) or 0.0

    factors: List[Factor] = []
    scale = atr_value or max(abs(price) * 0.002, 1e-9)

    factors.append(Factor(
        "trend_structure",
        _clamp((price - ema50) / (2 * scale)) * 0.6 + _clamp((ema50 - ema200) / (3 * scale)) * 0.4,
        2.0,
        f"price {price:.4f} vs EMA50 {ema50:.4f} vs EMA200 {ema200:.4f}",
    ))
    factors.append(Factor(
        "ema_cross",
        _clamp((ema20 - ema50) / (1.5 * scale)),
        1.5,
        f"EMA20 {ema20:.4f} vs EMA50 {ema50:.4f}",
    ))
    factors.append(Factor(
        "ema_slope",
        _clamp(ema20_slope / (0.3 * scale)),
        1.0,
        f"EMA20 slope {ema20_slope:+.5f}/bar",
    ))
    factors.append(Factor(
        "macd",
        _clamp(histogram_value / (0.6 * scale)),
        1.0,
        f"MACD histogram {histogram_value:+.5f}",
    ))
    factors.append(Factor(
        "rsi",
        _clamp((rsi_value - 50.0) / 25.0),
        1.0,
        f"RSI(14) {rsi_value:.1f}",
    ))

    di_total = di_plus + di_minus
    di_edge = (di_plus - di_minus) / di_total if di_total else 0.0
    directional = _clamp(di_edge) * _clamp(adx_value / 40.0, 0.0, 1.0)
    factors.append(Factor(
        "adx_direction",
        directional,
        1.5,
        f"ADX {adx_value:.1f}, +DI {di_plus:.1f} / -DI {di_minus:.1f}",
    ))

    window = candles[-20:]
    high = max(candle.high for candle in window)
    low = min(candle.low for candle in window)
    span = high - low
    position = ((price - low) / span * 2 - 1) if span else 0.0
    factors.append(Factor(
        "range_position",
        _clamp(position),
        0.75,
        f"{(position + 1) / 2:.0%} of the 20-bar range ({low:.4f}-{high:.4f})",
    ))

    lookback = min(10, len(closes) - 1)
    momentum = (price - closes[-1 - lookback]) / (lookback * scale) if lookback else 0.0
    factors.append(Factor(
        "momentum",
        _clamp(momentum),
        0.75,
        f"{lookback}-bar change {price - closes[-1 - lookback]:+.4f}",
    ))

    total_weight = sum(factor.weight for factor in factors)
    score = sum(factor.value * factor.weight for factor in factors) / total_weight * 100.0

    if score >= 20:
        bias = Bias.BULLISH
    elif score <= -20:
        bias = Bias.BEARISH
    else:
        bias = Bias.NEUTRAL

    # Agreement across factors matters as much as the headline score: eight
    # weak-but-aligned readings beat one extreme outlier.
    aligned = sum(
        factor.weight for factor in factors
        if (factor.value > 0.05 and score > 0) or (factor.value < -0.05 and score < 0)
    )
    agreement = aligned / total_weight if total_weight else 0.0
    confidence = min(100.0, abs(score) * 0.6 + agreement * 40.0 + min(adx_value, 40.0) * 0.5)

    return TechnicalRead(
        bias=bias,
        score=score,
        confidence=confidence,
        strength=trend_strength(adx_value, management),
        price=price,
        atr=atr_value,
        adx=adx_value,
        rsi=rsi_value,
        ema_fast=ema20,
        ema_slow=ema50,
        ema_trend=ema200,
        factors=factors,
    )
