"""What can this account afford to trade at all?

A small account meets the broker's smallest lot before it meets any signal: if
even the minimum lot loses more at the stop than the risk limit allows, every
trade on that instrument is rejected, however good the setup.  This works out,
for one instrument and the current equity, where that line is.  It uses the
SAME rule the bot already enforces (never force the minimum lot over the limit,
never raise the risk %, never widen the stop) and changes none of it; it only
says in advance what the rule will do.

For one instrument, with ``lot`` the broker's minimum lot and ``value`` the
account-currency value of a 1.0 price move on 1 lot:

    loss(stop)      = (stop + half spread + slippage) x value x lot
                      + commission_per_lot x lot
    max stop        = the stop distance where loss(stop) = the risk limit
    tightest stop   = analysis.min_stop_atr x ATR -- the smallest stop the bot
                      ever uses (it never goes tighter, and never widens one)
    affordable      = max stop >= tightest stop
    minimum equity  = loss(stop) / risk %

If the value, the lot or the ATR is unknown the answer is "unknown", never a
guess, and nothing is skipped on the strength of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Afford:
    epic: str
    equity: float
    risk_percent: float
    max_money: float                       # the most one trade may lose
    known: bool = False                    # enough inputs to judge
    why_unknown: str = ""                  # no_value | no_lot | no_atr | no_equity
    min_lot: float = 0.0
    value: float = 0.0                     # money per 1.0 price move per 1.0 lot
    atr: float = 0.0
    cost: float = 0.0                      # half spread + slippage, in price
    commission: float = 0.0                # for the minimum lot
    tight_stop: float = 0.0                # analysis.min_stop_atr x ATR
    max_stop: float = 0.0                  # the widest stop the minimum lot can take
    tight_loss: float = 0.0                # money lost at the tightest stop
    min_equity_tight: float = 0.0          # equity needed for the tightest stop
    typical_stop: Optional[float] = None   # the latest plan's stop, if there is one
    typical_loss: Optional[float] = None
    min_equity_typical: Optional[float] = None

    @property
    def affordable(self) -> bool:
        """True when the minimum lot fits the risk limit at the tightest stop."""
        return self.known and self.max_stop >= self.tight_stop - 1e-12

    @property
    def unaffordable(self) -> bool:
        return self.known and not self.affordable

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def loss_at(stop: float, *, value: float, lot: float, cost: float, commission: float) -> float:
    """Money lost at ``stop`` (a price distance) by the minimum lot, costs included."""
    return (stop + cost) * value * lot + commission


def assess(
    *,
    epic: str,
    equity: Optional[float],
    risk_percent: float,
    value_per_point: Optional[float],
    min_lot: Optional[float],
    atr: Optional[float],
    min_stop_atr: float,
    spread: float = 0.0,
    slippage: float = 0.0,
    commission_per_lot: float = 0.0,
    typical_stop: Optional[float] = None,
) -> Afford:
    result = Afford(epic=epic, equity=float(equity or 0.0), risk_percent=risk_percent,
                    max_money=float(equity or 0.0) * risk_percent / 100.0)
    if not equity or equity <= 0 or risk_percent <= 0:
        result.why_unknown = "no_equity"
        return result
    if not value_per_point or value_per_point <= 0:
        result.why_unknown = "no_value"
        return result
    if not min_lot or min_lot <= 0:
        result.why_unknown = "no_lot"
        return result
    if not atr or atr <= 0:
        result.why_unknown = "no_atr"
        return result

    cost = max(0.0, spread) / 2.0 + max(0.0, slippage)
    commission = max(0.0, commission_per_lot) * min_lot
    per_unit = value_per_point * min_lot
    result.known = True
    result.min_lot, result.value, result.atr = min_lot, value_per_point, atr
    result.cost, result.commission = cost, commission
    result.tight_stop = max(0.0, min_stop_atr) * atr
    result.max_stop = (result.max_money - commission) / per_unit - cost
    kw = dict(value=value_per_point, lot=min_lot, cost=cost, commission=commission)
    result.tight_loss = loss_at(result.tight_stop, **kw)
    result.min_equity_tight = result.tight_loss / (risk_percent / 100.0)
    if typical_stop and typical_stop > 0:
        result.typical_stop = typical_stop
        result.typical_loss = loss_at(typical_stop, **kw)
        result.min_equity_typical = result.typical_loss / (risk_percent / 100.0)
    return result


def minimum_equity(min_lot_money: float, risk_percent: float) -> Optional[float]:
    """Equity at which a loss of ``min_lot_money`` is exactly ``risk_percent`` of it."""
    if min_lot_money <= 0 or risk_percent <= 0:
        return None
    return min_lot_money / (risk_percent / 100.0)
