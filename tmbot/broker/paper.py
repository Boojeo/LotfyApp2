"""In-memory broker used by the tests and by ``--offline`` dry runs.

It mimics a netting account: an opposite-direction deal reduces the position
rather than opening a second one.
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

from ..errors import PermanentError
from ..models import BrokerPosition, Candle, Direction, MarketRules, Quote, utcnow
from .base import BrokerAdapter, PartialCloseProbe, PartialCloseStrategy


class PaperBroker(BrokerAdapter):
    name = "paper"

    def __init__(
        self,
        *,
        rules: Optional[Dict[str, MarketRules]] = None,
        candles: Optional[Dict[str, List[Candle]]] = None,
    ):
        self._positions: Dict[str, BrokerPosition] = {}
        self._quotes: Dict[str, Quote] = {}
        self._rules = rules or {}
        self._candles = candles or {}
        self._confirms: Dict[str, Dict[str, Any]] = {}
        self._refs = itertools.count(1)
        self.calls: List[tuple[str, tuple, dict]] = []
        self.hedging = False
        self.fail_next: Optional[Exception] = None

    # ------------------------------------------------------------------ helpers

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error

    def _next_ref(self) -> str:
        return f"ref-{next(self._refs)}"

    def seed_position(self, position: BrokerPosition) -> BrokerPosition:
        self._positions[position.deal_id] = position
        return position

    def set_quote(self, epic: str, bid: float, ask: Optional[float] = None) -> Quote:
        quote = Quote(epic=epic, bid=bid, ask=ask if ask is not None else bid, ts=utcnow())
        self._quotes[epic] = quote
        return quote

    def set_candles(self, epic: str, timeframe: str, candles: List[Candle]) -> None:
        self._candles[f"{epic}:{timeframe}"] = candles

    def set_rules(self, rules: MarketRules) -> None:
        self._rules[rules.epic] = rules

    # ------------------------------------------------------------------ interface

    def connect(self) -> None:
        self._record("connect")

    def account_summary(self) -> Dict[str, Any]:
        return {"accountId": "PAPER", "currency": "USD", "balance": {"balance": 10_000.0}}

    def hedging_mode(self) -> Optional[bool]:
        return self.hedging

    def positions(self) -> List[BrokerPosition]:
        self._record("positions")
        return list(self._positions.values())

    def position(self, deal_id: str) -> Optional[BrokerPosition]:
        return self._positions.get(deal_id)

    def market_rules(self, epic: str) -> MarketRules:
        return self._rules.get(epic, MarketRules(epic=epic, min_deal_size=0.01, size_step=0.01))

    def quote(self, epic: str) -> Quote:
        if epic not in self._quotes:
            raise PermanentError(f"no quote seeded for {epic}")
        return self._quotes[epic]

    def candles(self, epic: str, timeframe: str, limit: int) -> List[Candle]:
        return self._candles.get(f"{epic}:{timeframe}", [])[-limit:]

    def modify_position(
        self,
        deal_id: str,
        *,
        stop_level: Optional[float] = None,
        profit_level: Optional[float] = None,
    ) -> str:
        self._record("modify_position", deal_id, stop_level=stop_level, profit_level=profit_level)
        current = self._positions.get(deal_id)
        if current is None:
            raise PermanentError(f"position {deal_id} not found", code="error.position.notfound")
        updated = BrokerPosition(
            deal_id=current.deal_id,
            epic=current.epic,
            direction=current.direction,
            size=current.size,
            entry_price=current.entry_price,
            stop_level=current.stop_level if stop_level is None else stop_level,
            profit_level=current.profit_level if profit_level is None else profit_level,
            currency=current.currency,
            created_at=current.created_at,
        )
        self._positions[deal_id] = updated
        ref = self._next_ref()
        self._confirms[ref] = {"dealStatus": "ACCEPTED", "dealId": deal_id}
        return ref

    def close_position(self, deal_id: str, size: Optional[float] = None) -> str:
        self._record("close_position", deal_id, size=size)
        current = self._positions.get(deal_id)
        if current is None:
            raise PermanentError(f"position {deal_id} not found", code="error.position.notfound")
        closing = current.size if size is None else min(size, current.size)
        remaining = round(current.size - closing, 6)
        if remaining <= 0:
            del self._positions[deal_id]
        else:
            self._positions[deal_id] = BrokerPosition(
                deal_id=current.deal_id,
                epic=current.epic,
                direction=current.direction,
                size=remaining,
                entry_price=current.entry_price,
                stop_level=current.stop_level,
                profit_level=current.profit_level,
                currency=current.currency,
                created_at=current.created_at,
            )
        ref = self._next_ref()
        self._confirms[ref] = {"dealStatus": "ACCEPTED", "dealId": deal_id, "size": closing}
        return ref

    def open_position(
        self,
        epic: str,
        direction: Direction,
        size: float,
        *,
        stop_level: Optional[float] = None,
        profit_level: Optional[float] = None,
    ) -> str:
        self._record("open_position", epic, direction, size, stop_level=stop_level)
        if not self.hedging:
            # Netting: an opposite deal reduces the existing position.
            for deal_id, position in list(self._positions.items()):
                if position.epic == epic and position.direction is direction.opposite:
                    return self.close_position(deal_id, size=size)
        deal_id = f"paper-{next(self._refs)}"
        quote = self._quotes.get(epic)
        entry = quote.ask if quote and direction is Direction.BUY else (quote.bid if quote else 0.0)
        self._positions[deal_id] = BrokerPosition(
            deal_id=deal_id, epic=epic, direction=direction, size=size,
            entry_price=entry, stop_level=stop_level, profit_level=profit_level,
            created_at=utcnow(),
        )
        ref = self._next_ref()
        self._confirms[ref] = {"dealStatus": "ACCEPTED", "dealId": deal_id}
        return ref

    def confirm(self, deal_reference: str) -> Dict[str, Any]:
        return self._confirms.get(deal_reference, {"dealStatus": "UNKNOWN"})

    def set_hedging_mode(self, enabled: bool) -> Dict[str, Any]:
        self._record("set_hedging_mode", enabled)
        self.hedging = enabled
        return {"status": "SUCCESS"}

    def search_markets(self, term: str) -> List[Dict[str, Any]]:
        return [
            {"epic": epic, "instrumentName": epic, "marketStatus": "TRADEABLE"}
            for epic in sorted(self._rules)
            if term.lower() in epic.lower()
        ]

    def probe_partial_close(self, *, needed: bool = True) -> PartialCloseProbe:
        strategy = (
            PartialCloseStrategy.NETTING_OFFSET if not self.hedging
            else PartialCloseStrategy.DELETE_WITH_SIZE
        )
        self.partial_close_strategy = strategy
        return PartialCloseProbe(
            strategy=strategy,
            hedging_mode=self.hedging,
            delete_accepts_size=True,
            notes=["paper broker: partial closes are exact"],
            needed=needed,
        )
