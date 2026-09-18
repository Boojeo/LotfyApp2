"""Broker abstraction.

The trade engine only ever sees this interface, which keeps the management
logic testable against :class:`~tmbot.broker.paper.PaperBroker` and portable to
another venue later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from ..models import BrokerPosition, Candle, Direction, MarketRules, Quote


class PartialCloseStrategy(str, Enum):
    """How a fraction of an open position gets closed on this account.

    ``NETTING_OFFSET`` is preferred whenever the account is in netting mode: an
    opposite-direction deal of exactly the partial size can only ever reduce the
    position, whereas a ``DELETE`` whose ``size`` field is silently ignored
    would close the whole thing -- turning a partial take-profit into a full
    exit and throwing away the runner.
    """

    DELETE_WITH_SIZE = "DELETE_WITH_SIZE"
    NETTING_OFFSET = "NETTING_OFFSET"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass
class PartialCloseProbe:
    strategy: PartialCloseStrategy
    hedging_mode: Optional[bool]
    delete_accepts_size: Optional[bool]
    notes: List[str]

    def render(self) -> str:
        lines = ["[startup] capability probe: partial_close"]
        lines.extend(f"  -> {note}" for note in self.notes)
        verdict = "ok" if self.strategy is not PartialCloseStrategy.UNSUPPORTED else "FAIL"
        lines.append(f"  [{verdict}] partial close strategy = {self.strategy.value}")
        return "\n".join(lines)


class BrokerAdapter(ABC):
    """Read market state, read positions, modify stops/targets, close size."""

    name: str = "broker"

    @abstractmethod
    def connect(self) -> None:
        """Authenticate and prepare the adapter for use."""

    def close(self) -> None:
        """Release resources.  Default is a no-op."""

    @abstractmethod
    def account_summary(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    def hedging_mode(self) -> Optional[bool]:
        """True if the account books opposite deals separately instead of netting."""

    @abstractmethod
    def positions(self) -> List[BrokerPosition]:
        ...

    @abstractmethod
    def position(self, deal_id: str) -> Optional[BrokerPosition]:
        ...

    @abstractmethod
    def market_rules(self, epic: str) -> MarketRules:
        ...

    @abstractmethod
    def quote(self, epic: str) -> Quote:
        ...

    @abstractmethod
    def candles(self, epic: str, timeframe: str, limit: int) -> List[Candle]:
        ...

    @abstractmethod
    def modify_position(
        self,
        deal_id: str,
        *,
        stop_level: Optional[float] = None,
        profit_level: Optional[float] = None,
    ) -> str:
        """Set stop and/or target.  Returns a deal reference."""

    @abstractmethod
    def close_position(self, deal_id: str, size: Optional[float] = None) -> str:
        """Close ``size`` of the position, or all of it when ``size`` is None."""

    @abstractmethod
    def open_position(
        self,
        epic: str,
        direction: Direction,
        size: float,
        *,
        stop_level: Optional[float] = None,
        profit_level: Optional[float] = None,
    ) -> str:
        """Used only by the netting-offset partial close path."""

    @abstractmethod
    def confirm(self, deal_reference: str) -> Dict[str, Any]:
        """Resolve a deal reference into its acceptance/rejection record."""

    @abstractmethod
    def probe_partial_close(self) -> PartialCloseProbe:
        ...

    def search_markets(self, term: str) -> List[Dict[str, Any]]:
        """Find instruments by name so a user can discover the epic to trade."""
        raise NotImplementedError(f"{self.name} cannot search markets")
