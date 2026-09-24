"""Pick the broker adapter that matches ``broker.platform``."""

from __future__ import annotations

from ..config import BrokerConfig
from .base import BrokerAdapter


def build_broker(config: BrokerConfig) -> BrokerAdapter:
    if config.platform == "mt5":
        from .mt5 import MT5Broker
        return MT5Broker(config)
    from .capital import CapitalComBroker
    return CapitalComBroker(config)
