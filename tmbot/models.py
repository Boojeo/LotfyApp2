"""Core value objects shared by every layer.

These are deliberately plain dataclasses with explicit ``to_dict``/``from_dict``
so they can round-trip through SQLite without an ORM.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        # Capital.com streams epoch milliseconds in some payloads.
        seconds = raw / 1000.0 if raw > 1e11 else float(raw)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(raw).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for a long, -1 for a short.  Lets one formula serve both sides."""
        return 1 if self is Direction.BUY else -1

    @property
    def opposite(self) -> "Direction":
        return Direction.SELL if self is Direction.BUY else Direction.BUY

    def is_beyond(self, price: float, level: float) -> bool:
        """True when ``price`` has reached ``level`` in the profitable direction."""
        return price >= level if self is Direction.BUY else price <= level


class Bias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    @property
    def direction(self) -> Optional[Direction]:
        if self is Bias.BULLISH:
            return Direction.BUY
        if self is Bias.BEARISH:
            return Direction.SELL
        return None


class TrendStrength(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"


class TradeStatus(str, Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    MANAGING = "MANAGING"
    CLOSED = "CLOSED"
    DECLINED = "DECLINED"
    ERROR = "ERROR"


class Stage(str, Enum):
    """Which rung of the take-profit ladder a decision belongs to."""

    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_capital(cls, raw: Dict[str, Any]) -> "Candle":
        """Build from a Capital.com ``/prices`` bar.

        Each OHLC field arrives as ``{"bid": x, "ask": y}``; we use the mid so
        indicators are not biased by the spread.
        """

        def mid(node: Any) -> float:
            if isinstance(node, dict):
                bid, ask = node.get("bid"), node.get("ask")
                if bid is not None and ask is not None:
                    return (float(bid) + float(ask)) / 2.0
                return float(bid if bid is not None else ask)
            return float(node)

        return cls(
            ts=_parse_dt(raw.get("snapshotTimeUTC") or raw.get("snapshotTime")),
            open=mid(raw["openPrice"]),
            high=mid(raw["highPrice"]),
            low=mid(raw["lowPrice"]),
            close=mid(raw["closePrice"]),
            volume=float(raw.get("lastTradedVolume") or 0.0),
        )


@dataclass(frozen=True)
class Quote:
    epic: str
    bid: float
    ask: float
    ts: datetime = field(default_factory=utcnow)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def exit_price(self, direction: Direction) -> float:
        """The price a position of ``direction`` would actually be closed at."""
        return self.bid if direction is Direction.BUY else self.ask

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        return ((now or utcnow()) - self.ts).total_seconds()


@dataclass(frozen=True)
class MarketRules:
    """Dealing constraints for one instrument.

    Sourced from ``GET /api/v1/markets/{epic}``.  Every level and size we send
    is rounded/validated against these, because a rejected modify is a silent
    failure to protect the trade.
    """

    epic: str
    name: str = ""
    min_deal_size: float = 0.0
    size_step: float = 0.0
    decimal_places: int = 2
    min_stop_distance: float = 0.0
    min_stop_distance_is_pct: bool = False
    tradeable: bool = True

    def round_price(self, price: float) -> float:
        return round(price, self.decimal_places)

    def round_size(self, size: float) -> float:
        """Round a size *down* to a legal increment (never over-close)."""
        if self.size_step and self.size_step > 0:
            steps = math.floor(round(size / self.size_step, 9))
            size = steps * self.size_step
        # Sizes are quoted to at most 4 dp by Capital.com.
        return round(size, 4)

    def stop_buffer(self, reference_price: float) -> float:
        """Minimum absolute distance a stop must keep from the current price."""
        if self.min_stop_distance_is_pct:
            return abs(reference_price) * self.min_stop_distance / 100.0
        return self.min_stop_distance

    @classmethod
    def from_capital(cls, raw: Dict[str, Any]) -> "MarketRules":
        instrument = raw.get("instrument", raw)
        snapshot = raw.get("snapshot", {})
        rules = raw.get("dealingRules", {})

        def rule(name: str) -> tuple[float, bool]:
            node = rules.get(name) or {}
            value = node.get("value")
            unit = str(node.get("unit", "POINTS")).upper()
            return (float(value) if value is not None else 0.0, unit == "PERCENTAGE")

        min_size, _ = rule("minDealSize")
        step, _ = rule("minSizeIncrement")
        stop_distance, stop_is_pct = rule("minStopOrProfitDistance")
        if not stop_distance:
            stop_distance, stop_is_pct = rule("minNormalStopOrProfitDistance")

        decimals = snapshot.get("decimalPlacesFactor")
        return cls(
            epic=instrument.get("epic", raw.get("epic", "")),
            name=instrument.get("name", ""),
            min_deal_size=min_size,
            size_step=step,
            decimal_places=int(decimals) if decimals is not None else 2,
            min_stop_distance=stop_distance,
            min_stop_distance_is_pct=stop_is_pct,
            tradeable=str(snapshot.get("marketStatus", "TRADEABLE")).upper() == "TRADEABLE",
        )


@dataclass(frozen=True)
class BrokerPosition:
    """An open position as the broker currently sees it."""

    deal_id: str
    epic: str
    direction: Direction
    size: float
    entry_price: float
    stop_level: Optional[float] = None
    profit_level: Optional[float] = None
    currency: str = ""
    created_at: Optional[datetime] = None
    upl: Optional[float] = None

    @classmethod
    def from_capital(cls, raw: Dict[str, Any]) -> "BrokerPosition":
        position = raw.get("position", raw)
        market = raw.get("market", {})
        return cls(
            deal_id=position["dealId"],
            epic=position.get("epic") or market.get("epic", ""),
            direction=Direction(position["direction"]),
            size=float(position["size"]),
            entry_price=float(position.get("level")),
            stop_level=_opt_float(position.get("stopLevel")),
            profit_level=_opt_float(position.get("profitLevel")),
            currency=position.get("currency", ""),
            created_at=_parse_dt(position.get("createdDateUTC") or position.get("createdDate")),
            upl=_opt_float(position.get("upl")),
        )


def _opt_float(value: Any) -> Optional[float]:
    return None if value in (None, "") else float(value)


@dataclass
class Level:
    """A support/resistance or liquidity zone distilled to one price."""

    price: float
    kind: str  # "support" | "resistance"
    score: float = 0.0
    touches: int = 0
    is_liquidity: bool = False  # equal highs/lows: a stop pool
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": self.price,
            "kind": self.kind,
            "score": round(self.score, 3),
            "touches": self.touches,
            "is_liquidity": self.is_liquidity,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Level":
        return cls(**raw)


@dataclass
class TradePlan:
    """The daily analytical output: a bias plus exactly three targets and a stop."""

    epic: str
    created_at: datetime
    bias: Bias
    direction: Direction
    confidence: float
    reference_price: float
    tp1: float
    tp2: float
    tp3: float
    sl: float
    atr: float
    technical: Dict[str, Any] = field(default_factory=dict)
    fundamental: Dict[str, Any] = field(default_factory=dict)
    levels: List[Level] = field(default_factory=list)
    headlines: List[Dict[str, Any]] = field(default_factory=list)
    narrative: str = ""
    advisory_only: bool = False
    plan_id: str = ""

    @property
    def targets(self) -> List[float]:
        return [self.tp1, self.tp2, self.tp3]

    @property
    def risk(self) -> float:
        return abs(self.reference_price - self.sl)

    def reward_risk(self, target: float) -> float:
        return abs(target - self.reference_price) / self.risk if self.risk else 0.0

    def rebase(self, entry_price: float) -> "TradePlan":
        """Re-anchor the plan onto the price you actually filled at.

        Targets are structural (they stay put); the stop keeps its *distance*
        from the reference, so a worse fill does not silently widen risk.
        """
        shift = entry_price - self.reference_price
        return TradePlan(
            epic=self.epic,
            created_at=self.created_at,
            bias=self.bias,
            direction=self.direction,
            confidence=self.confidence,
            reference_price=entry_price,
            tp1=self.tp1,
            tp2=self.tp2,
            tp3=self.tp3,
            sl=self.sl + shift,
            atr=self.atr,
            technical=dict(self.technical),
            fundamental=dict(self.fundamental),
            levels=list(self.levels),
            headlines=list(self.headlines),
            narrative=self.narrative,
            advisory_only=self.advisory_only,
            plan_id=self.plan_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "epic": self.epic,
            "created_at": _iso(self.created_at),
            "bias": self.bias.value,
            "direction": self.direction.value,
            "confidence": self.confidence,
            "reference_price": self.reference_price,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "sl": self.sl,
            "atr": self.atr,
            "technical": self.technical,
            "fundamental": self.fundamental,
            "levels": [lvl.to_dict() for lvl in self.levels],
            "headlines": self.headlines,
            "narrative": self.narrative,
            "advisory_only": self.advisory_only,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TradePlan":
        return cls(
            epic=raw["epic"],
            created_at=_parse_dt(raw["created_at"]),
            bias=Bias(raw["bias"]),
            direction=Direction(raw["direction"]),
            confidence=float(raw["confidence"]),
            reference_price=float(raw["reference_price"]),
            tp1=float(raw["tp1"]),
            tp2=float(raw["tp2"]),
            tp3=float(raw["tp3"]),
            sl=float(raw["sl"]),
            atr=float(raw.get("atr", 0.0)),
            technical=raw.get("technical", {}),
            fundamental=raw.get("fundamental", {}),
            levels=[Level.from_dict(l) for l in raw.get("levels", [])],
            headlines=raw.get("headlines", []),
            narrative=raw.get("narrative", ""),
            advisory_only=bool(raw.get("advisory_only", False)),
            plan_id=raw.get("plan_id", ""),
        )


@dataclass
class ManagedTrade:
    """Live management state for one adopted position.

    ``remaining_size`` is refreshed from the broker on every cycle -- it is a
    cache, never the source of truth.  The ``*_done`` flags and ``stop_level``
    are ours and are what make the ladder idempotent across restarts.
    """

    deal_id: str
    epic: str
    direction: Direction
    entry_price: float
    initial_size: float
    remaining_size: float
    tp1: float
    tp2: float
    tp3: float
    sl: float
    plan_id: str = ""
    status: TradeStatus = TradeStatus.PENDING_CONFIRMATION
    tp1_done: bool = False
    tp2_done: bool = False
    breakeven_done: bool = False
    trailing_active: bool = False
    stop_level: Optional[float] = None
    best_price: Optional[float] = None  # highest high (long) / lowest low (short) since entry
    tp3_extensions: int = 0
    opened_at: Optional[datetime] = None
    adopted_at: datetime = field(default_factory=utcnow)
    closed_at: Optional[datetime] = None
    realised: float = 0.0
    note: str = ""

    @property
    def short_id(self) -> str:
        """A handle short enough to type into a chat command."""
        return self.deal_id[-6:] if len(self.deal_id) > 6 else self.deal_id

    @property
    def initial_risk(self) -> float:
        return abs(self.entry_price - self.sl)

    def update_best_price(self, price: float) -> None:
        if self.best_price is None:
            self.best_price = price
        elif self.direction is Direction.BUY:
            self.best_price = max(self.best_price, price)
        else:
            self.best_price = min(self.best_price, price)

    def r_multiple(self, price: float) -> float:
        risk = self.initial_risk
        if not risk:
            return 0.0
        return (price - self.entry_price) * self.direction.sign / risk

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "epic": self.epic,
            "direction": self.direction.value,
            "entry_price": self.entry_price,
            "initial_size": self.initial_size,
            "remaining_size": self.remaining_size,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "sl": self.sl,
            "plan_id": self.plan_id,
            "status": self.status.value,
            "tp1_done": self.tp1_done,
            "tp2_done": self.tp2_done,
            "breakeven_done": self.breakeven_done,
            "trailing_active": self.trailing_active,
            "stop_level": self.stop_level,
            "best_price": self.best_price,
            "tp3_extensions": self.tp3_extensions,
            "opened_at": _iso(self.opened_at),
            "adopted_at": _iso(self.adopted_at),
            "closed_at": _iso(self.closed_at),
            "realised": self.realised,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ManagedTrade":
        return cls(
            deal_id=raw["deal_id"],
            epic=raw["epic"],
            direction=Direction(raw["direction"]),
            entry_price=float(raw["entry_price"]),
            initial_size=float(raw["initial_size"]),
            remaining_size=float(raw["remaining_size"]),
            tp1=float(raw["tp1"]),
            tp2=float(raw["tp2"]),
            tp3=float(raw["tp3"]),
            sl=float(raw["sl"]),
            plan_id=raw.get("plan_id", ""),
            status=TradeStatus(raw.get("status", TradeStatus.MANAGING.value)),
            tp1_done=bool(raw.get("tp1_done")),
            tp2_done=bool(raw.get("tp2_done")),
            breakeven_done=bool(raw.get("breakeven_done")),
            trailing_active=bool(raw.get("trailing_active")),
            stop_level=_opt_float(raw.get("stop_level")),
            best_price=_opt_float(raw.get("best_price")),
            tp3_extensions=int(raw.get("tp3_extensions", 0)),
            opened_at=_parse_dt(raw.get("opened_at")),
            adopted_at=_parse_dt(raw.get("adopted_at")) or utcnow(),
            closed_at=_parse_dt(raw.get("closed_at")),
            realised=float(raw.get("realised", 0.0)),
            note=raw.get("note", ""),
        )

    @classmethod
    def from_position(cls, position: BrokerPosition, plan: TradePlan) -> "ManagedTrade":
        rebased = plan.rebase(position.entry_price)
        return cls(
            deal_id=position.deal_id,
            epic=position.epic,
            direction=position.direction,
            entry_price=position.entry_price,
            initial_size=position.size,
            remaining_size=position.size,
            tp1=rebased.tp1,
            tp2=rebased.tp2,
            tp3=rebased.tp3,
            sl=rebased.sl,
            plan_id=plan.plan_id,
            stop_level=position.stop_level,
            best_price=position.entry_price,
            opened_at=position.created_at,
        )


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything the rule engine is allowed to look at for one evaluation."""

    epic: str
    quote: Quote
    candles: List[Candle]
    atr: float
    adx: float
    ema_fast: float
    ema_slow: float
    strength: TrendStrength
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    rules: Optional[MarketRules] = None
