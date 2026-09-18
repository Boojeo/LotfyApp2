"""Shared fixtures for the test-suite."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from tmbot.config import ManagementConfig
from tmbot.models import (
    BrokerPosition,
    Candle,
    Direction,
    ManagedTrade,
    MarketRules,
    MarketSnapshot,
    Quote,
    TradeStatus,
    TrendStrength,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

RULES = MarketRules(
    epic="GOLD",
    name="Gold",
    min_deal_size=0.1,
    size_step=0.1,
    decimal_places=2,
    min_stop_distance=0.5,
)


def candles(
    count: int = 120,
    start: float = 3400.0,
    drift: float = 0.0,
    wave: float = 0.0,
    spread: float = 2.0,
    end: Optional[float] = None,
) -> List[Candle]:
    """Synthetic bars.  Pass ``end`` to make the series finish at a given price,
    which keeps a test's candle history consistent with its quote."""
    out: List[Candle] = []
    price = start if end is None else end - drift * (count - 1)
    for index in range(count):
        price += drift + (math.sin(index / 6) * wave)
        out.append(Candle(
            ts=BASE + timedelta(minutes=15 * index),
            open=price - spread / 4,
            high=price + spread,
            low=price - spread,
            close=price,
        ))
    return out


def trade(
    direction: Direction = Direction.BUY,
    *,
    entry: float = 3400.0,
    size: float = 1.0,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    tp3: Optional[float] = None,
    sl: Optional[float] = None,
    **overrides,
) -> ManagedTrade:
    sign = direction.sign
    managed = ManagedTrade(
        deal_id="deal-000001",
        epic="GOLD",
        direction=direction,
        entry_price=entry,
        initial_size=size,
        remaining_size=size,
        tp1=tp1 if tp1 is not None else entry + sign * 10,
        tp2=tp2 if tp2 is not None else entry + sign * 20,
        tp3=tp3 if tp3 is not None else entry + sign * 30,
        sl=sl if sl is not None else entry - sign * 10,
        status=TradeStatus.MANAGING,
        best_price=entry,
        opened_at=BASE,
        adopted_at=BASE,
    )
    managed.stop_level = managed.sl
    for key, value in overrides.items():
        setattr(managed, key, value)
    return managed


def snapshot(
    price: float,
    *,
    atr: float = 4.0,
    strength: TrendStrength = TrendStrength.MODERATE,
    swing_high: Optional[float] = None,
    swing_low: Optional[float] = None,
    bars: Optional[List[Candle]] = None,
    rules: Optional[MarketRules] = RULES,
    spread: float = 0.0,
) -> MarketSnapshot:
    return MarketSnapshot(
        epic="GOLD",
        quote=Quote(epic="GOLD", bid=price, ask=price + spread),
        candles=bars if bars is not None else [],
        atr=atr,
        adx=30.0 if strength is TrendStrength.STRONG else 20.0,
        ema_fast=price,
        ema_slow=price,
        strength=strength,
        swing_high=swing_high,
        swing_low=swing_low,
        rules=rules,
    )


def position(
    *,
    deal_id: str = "deal-000001",
    direction: Direction = Direction.BUY,
    size: float = 1.0,
    entry: float = 3400.0,
    stop: Optional[float] = None,
    target: Optional[float] = None,
) -> BrokerPosition:
    return BrokerPosition(
        deal_id=deal_id,
        epic="GOLD",
        direction=direction,
        size=size,
        entry_price=entry,
        stop_level=stop,
        profit_level=target,
        created_at=BASE,
    )


def management(**overrides) -> ManagementConfig:
    config = ManagementConfig()
    for key, value in overrides.items():
        setattr(config, key, value)
    return config
