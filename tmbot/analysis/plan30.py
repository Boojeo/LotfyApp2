"""30-day growth plan: a theoretical compounding curve beside the real results.

The 25% a day is arithmetic, not a forecast -- it turns $100 into about
$80,800 in 30 days.  So the plan keeps two ledgers that never touch:

* the *theoretical* one, compounded from the starting capital, used only to
  show how far reality is from the scenario;
* the *actual* one, built from the trades the bot recorded.  Day 1 starts at
  the plan's capital and every later day starts where the previous day really
  ended.

Nothing here opens a trade, changes a setting or pushes risk up to chase the
target.  A day with no trades is recorded as a no-trade day, never filled in.
Money is rounded to cents everywhere.

The state is a plain dict stored in the bot's database, so it survives a
restart and a finished day is never rewritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..models import Fill
from ..store import Store

KEY = "plan30"
DEFAULT_CAPITAL = 100.0
DEFAULT_TARGET = 0.25        # theoretical daily growth
DEFAULT_LOSS_LIMIT = 0.03    # share of the day's starting balance
DEFAULT_RISK = 0.01          # share of the balance risked per trade
DAYS = 30

# Rules a day can break; the keys are translated where they are shown.
RULE_LOSS_LIMIT = "loss_limit"      # the day lost more than the daily loss limit
RULE_TRADE_RISK = "trade_risk"      # one trade lost more than the per-trade risk

CONTINUE, REDUCE, PAUSE = "continue", "reduce", "pause"


def money(value: float) -> float:
    """Round to cents the way a spreadsheet does (half away from zero).

    Python's own ``round`` is banker's rounding on a binary float, so 61.035
    comes out 61.03 where Excel's ROUND says 61.04 -- the bot and the exported
    sheet would then disagree by a cent.  Trimming float noise first and
    rounding half-up makes them agree.
    """
    return float(Decimal(repr(round(float(value), 9))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class DayLog:
    """What happened on one calendar day of the plan."""

    net: Optional[float] = None         # actual net profit/loss; None = not known
    trades: int = 0
    wins: int = 0
    losses: int = 0
    costs: float = 0.0                  # commission + swap the broker booked
    max_dd: Optional[float] = None      # worst realised drop within the day, %
    estimated: bool = False             # some P/L was price move x lots, not booked money
    manual: bool = False                # typed in with /plan30 result
    final: bool = False                 # reviewed; never rewritten
    note: str = ""
    loss_limit_hit: bool = False
    broken: List[str] = field(default_factory=list)
    decision: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DayLog":
        known = {key: raw[key] for key in cls.__dataclass_fields__ if key in raw}
        return cls(**known)


@dataclass
class Plan30:
    start_date: date
    capital: float = DEFAULT_CAPITAL
    target: float = DEFAULT_TARGET
    loss_limit: float = DEFAULT_LOSS_LIMIT
    risk: float = DEFAULT_RISK
    days: int = DAYS
    currency: str = "USD"
    log: Dict[int, DayLog] = field(default_factory=dict)
    final_sent: bool = False
    limit_alerted: List[int] = field(default_factory=list)
    pending_alerted: List[int] = field(default_factory=list)

    def day(self, number: int) -> DayLog:
        return self.log.setdefault(number, DayLog())

    def day_number(self, today: date) -> int:
        """1-based day of the plan for ``today``; 0 before it starts."""
        return (today - self.start_date).days + 1

    def date_of(self, number: int) -> date:
        return date.fromordinal(self.start_date.toordinal() + number - 1)

    # ------------------------------------------------------------------ storage

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(), "capital": self.capital,
            "target": self.target, "loss_limit": self.loss_limit, "risk": self.risk,
            "days": self.days, "currency": self.currency,
            "log": {str(number): day.to_dict() for number, day in self.log.items()},
            "final_sent": self.final_sent, "limit_alerted": self.limit_alerted,
            "pending_alerted": self.pending_alerted,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Plan30":
        return cls(
            start_date=date.fromisoformat(raw["start_date"]),
            capital=float(raw.get("capital", DEFAULT_CAPITAL)),
            target=float(raw.get("target", DEFAULT_TARGET)),
            loss_limit=float(raw.get("loss_limit", DEFAULT_LOSS_LIMIT)),
            risk=float(raw.get("risk", DEFAULT_RISK)),
            days=int(raw.get("days", DAYS)), currency=raw.get("currency", "USD"),
            log={int(n): DayLog.from_dict(d) for n, d in (raw.get("log") or {}).items()},
            final_sent=bool(raw.get("final_sent", False)),
            limit_alerted=list(raw.get("limit_alerted", [])),
            pending_alerted=list(raw.get("pending_alerted", [])),
        )

    @classmethod
    def load(cls, store: Store) -> Optional["Plan30"]:
        raw = store.get(KEY)
        if not raw:
            return None
        try:
            return cls.from_dict(json.loads(raw))
        except (ValueError, KeyError):
            return None

    def save(self, store: Store) -> None:
        store.set(KEY, json.dumps(self.to_dict()))


# ---------------------------------------------------------------------- the table

@dataclass
class Row:
    number: int
    day: date
    actual_start: Optional[float]
    theoretical_start: float
    theoretical_profit: float
    theoretical_end: float
    net: Optional[float]
    actual_end: Optional[float]
    achieved: Optional[bool]
    trades: int
    note: str
    log: DayLog


def theoretical_curve(plan: Plan30) -> List[Tuple[float, float, float]]:
    """(start, target profit, end) per day, compounded and rounded to cents."""
    curve, balance = [], money(plan.capital)
    for _ in range(plan.days):
        profit = money(balance * plan.target)
        end = money(balance + profit)
        curve.append((balance, profit, end))
        balance = end
    return curve


def rows(plan: Plan30) -> List[Row]:
    """The 30-day table.  Actual figures are only ever filled from real results."""
    out: List[Row] = []
    curve = theoretical_curve(plan)
    actual_start: Optional[float] = money(plan.capital)
    for number in range(1, plan.days + 1):
        log = plan.log.get(number) or DayLog()
        t_start, t_profit, t_end = curve[number - 1]
        net = log.net
        actual_end = (money(actual_start + net)
                      if actual_start is not None and net is not None else None)
        achieved = None
        if net is not None and actual_start is not None:
            achieved = net >= money(actual_start * plan.target)
        out.append(Row(number, plan.date_of(number), actual_start, t_start, t_profit,
                       t_end, net, actual_end, achieved, log.trades, log.note, log))
        actual_start = actual_end
    return out


def actual_balance(plan: Plan30) -> float:
    """The real balance after every day recorded so far."""
    balance = money(plan.capital)
    for row in rows(plan):
        if row.actual_end is None:
            break
        balance = row.actual_end
    return balance


# ---------------------------------------------------------------------- a day

ValueOf = Callable[[str], Optional[float]]


def evaluate_day(
    fills: Sequence[Fill],
    plan: Plan30,
    number: int,
    start_balance: float,
    tz: tzinfo,
    value_of: ValueOf,
) -> Optional[DayLog]:
    """Net result of the fills that closed on day ``number``.

    The money a fill made is what the broker booked, when it says.  When it
    does not, it is estimated as price move x lots x contract value, and the
    day is marked estimated -- spread and fees are then only as good as the
    prices.  If even that cannot be worked out the day is *not known* (None)
    and has to be entered by hand; a number is never invented.
    """
    day = plan.date_of(number)
    todays = [f for f in fills if f.ts.astimezone(tz).date() == day]
    log = DayLog()
    if not todays:
        log.net = 0.0
        return log

    per_deal: Dict[str, float] = {}
    sequence: List[float] = []
    for fill in sorted(todays, key=lambda f: f.ts):
        costs = (fill.commission or 0.0) + (fill.swap or 0.0)
        if fill.profit is not None:
            gross = fill.profit
        else:
            value = value_of(fill.epic)
            if not value:
                return None
            gross = (fill.price - fill.entry_price) * fill.direction.sign * fill.size * value
            log.estimated = True
        result = gross + costs
        log.costs += costs
        per_deal[fill.deal_id] = per_deal.get(fill.deal_id, 0.0) + result
        sequence.append(result)

    log.trades = len(per_deal)
    log.wins = sum(1 for r in per_deal.values() if r > 0)
    log.losses = sum(1 for r in per_deal.values() if r <= 0)
    log.net = money(sum(sequence))
    log.costs = money(log.costs)
    log.max_dd = _max_drawdown(start_balance, sequence)

    worst = min(per_deal.values())
    if log.net < -money(start_balance * plan.loss_limit):
        log.broken.append(RULE_LOSS_LIMIT)
    if worst < -money(start_balance * plan.risk):
        log.broken.append(RULE_TRADE_RISK)
    log.loss_limit_hit = log.net <= -money(start_balance * plan.loss_limit)
    return log


def _max_drawdown(start: float, results: Sequence[float]) -> float:
    """Largest peak-to-trough drop of the balance across the day's closed results, %."""
    balance = peak = start
    worst = 0.0
    for result in results:
        balance += result
        peak = max(peak, balance)
        if peak > 0:
            worst = max(worst, (peak - balance) / peak * 100.0)
    return round(worst, 2)


def decide(plan: Plan30, number: int) -> str:
    """continue / reduce risk / pause -- advice only, nothing is changed."""
    log = plan.day(number)
    if log.loss_limit_hit:
        return PAUSE
    if log.broken:
        return REDUCE
    previous = plan.log.get(number - 1)
    if (log.net is not None and log.net < 0
            and previous and previous.net is not None and previous.net < 0):
        return REDUCE
    return CONTINUE


def daily_limit(plan: Plan30, start_balance: float) -> float:
    """Most the day may lose before trading should stop."""
    return money(start_balance * plan.loss_limit)


# ---------------------------------------------------------------------- the end

def summary(plan: Plan30) -> Dict[str, Any]:
    """The figures of the final report, from the days recorded so far."""
    table = rows(plan)
    known = [row for row in table if row.net is not None]
    final = actual_balance(plan)
    wins = sum(row.log.wins for row in known)
    losses = sum(row.log.losses for row in known)
    drawdowns = [row.log.max_dd for row in known if row.log.max_dd is not None]
    reviewed = [row for row in known if row.log.final]
    last_theory = table[len(known) - 1].theoretical_end if known else money(plan.capital)
    return {
        "initial": money(plan.capital),
        "final": final,
        "net": money(final - plan.capital),
        "return_pct": round((final - plan.capital) / plan.capital * 100.0, 2),
        "theoretical_final": table[-1].theoretical_end,
        "theoretical_so_far": last_theory,
        "difference": money(table[-1].theoretical_end - final),
        "days_recorded": len(known),
        "wins": wins, "losses": losses,
        "win_rate": round(wins / (wins + losses) * 100.0, 1) if wins + losses else None,
        "max_drawdown": max(drawdowns) if drawdowns else None,
        "costs": money(sum(row.log.costs for row in known)),
        "no_trade_days": sum(1 for row in known if row.log.trades == 0),
        "days_reviewed": len(reviewed),
        "days_compliant": sum(1 for row in reviewed if not row.log.broken),
        "days_over_limit": sum(1 for row in reviewed if row.log.loss_limit_hit),
        "estimated_days": sum(1 for row in known if row.log.estimated),
    }
