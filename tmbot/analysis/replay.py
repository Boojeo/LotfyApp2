"""Replay the bot's decisions over past candles and measure what happened next.

``/why`` counts what stopped opportunities.  It cannot say whether stopping them
was right.  That needs the other half of the story: for every past decision, what
did price do afterwards?  This walks back through real history, rebuilds the
bot's plan exactly as it would have been at each moment (the real code, with only
candles up to that moment), and then looks at the candles that followed:

    for each past moment t:
        plan  = the bot's real plan using candles <= t only     (no look-ahead)
        later = the candles after t on the plan's entry timeframe
        did TP1 / TP2 / TP3 come before the stop, within ``horizon`` bars?

Then it groups the results by verdict, by each quality flag (present vs absent),
by signal strength, by alignment and by automatic-mode blocker, so you can see
for yourself which filters separate good setups from bad ones on YOUR data and
which just remove opportunities.

What it measures, and what it does not:

* the plan's own entry, stop and targets, followed on later candles; a bar that
  touches stop and target together counts as the stop;
* no spread on exit, no slippage, no trailing or break-even management, no
  daily-loss limit: it asks "was the setup sound?", not "what would the account
  have made?";
* consecutive moments overlap in time, so groups are smaller than their counts
  suggest and intervals are somewhat optimistic; a group with fewer than
  ``MIN_GROUP`` samples is shown without a verdict;
* past behaviour is not a forecast.

The ReplayBroker can only read candles, quotes, rules and equity: it has no
order functions at all, so a replay cannot place or touch a trade.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ..models import Candle, Direction, Quote
from . import funnel
from .odds import wilson

MIN_GROUP = 20
TIMEFRAMES = ("M5", "M15", "H1", "H4", "D1")


class ReplayBroker:
    """A time-travelling READ-ONLY view of a broker. It has no way to trade."""

    name = "replay"

    def __init__(self, real: Any, histories: Dict[str, List[Candle]], *,
                 equity: Optional[float] = None, spread: Optional[float] = None):
        self._real = real
        self._histories = histories
        self._equity = equity
        self._spread = spread
        self.cutoff: Optional[datetime] = None

    def set_time(self, ts: datetime) -> None:
        self.cutoff = ts

    def candles(self, epic: str, timeframe: str, limit: int) -> List[Candle]:
        bars = self._histories.get(timeframe, [])
        if self.cutoff is not None:
            bars = [c for c in bars if c.ts <= self.cutoff]
        return bars[-limit:]

    def quote(self, epic: str) -> Quote:
        finest = next((tf for tf in TIMEFRAMES if self._histories.get(tf)), None)
        bars = self.candles(epic, finest, 1) if finest else []
        if not bars:
            raise ValueError("no candles at that moment")
        spread = self._spread
        if spread is None:
            try:
                spread = self._real.quote(epic).spread
            except Exception:
                spread = 0.0
        price = bars[-1].close
        return Quote(epic=epic, bid=price - spread / 2.0, ask=price + spread / 2.0,
                     ts=bars[-1].ts)

    def market_rules(self, epic: str):
        return self._real.market_rules(epic)

    def account_summary(self) -> Dict[str, Any]:
        if self._equity is not None:
            summary = dict(self._real.account_summary() or {})
            summary["equity"] = self._equity
            summary["balance"] = self._equity
            return summary
        return self._real.account_summary()


def fetch_histories(broker: Any, epic: str, bars: int) -> Dict[str, List[Candle]]:
    out: Dict[str, List[Candle]] = {}
    for timeframe in TIMEFRAMES:
        try:
            out[timeframe] = list(broker.candles(epic, timeframe, bars))
        except Exception:
            out[timeframe] = []
    return out


# ---------------------------------------------------------------------- outcomes

def outcome(
    direction: Direction, entry: float, stop: float, targets: Sequence[float],
    future: Sequence[Candle], horizon: int,
) -> Dict[str, Any]:
    """Which of the stop and each target came first on ``future``; R per target.

    R is in units of the risk taken: a target reached first scores its distance
    in R, a stop first scores -1, and a trade still open at the horizon is marked
    to the last close.  Each target is judged on its own against the same stop.
    """
    sign = direction.sign
    risk = abs(entry - stop)
    window = list(future[:horizon])
    result: Dict[str, Any] = {"bars": len(window), "r": [], "state": []}
    if risk <= 0 or not window:
        return result
    last_close_r = (window[-1].close - entry) * sign / risk
    for target in targets:
        reward = abs(target - entry) / risk
        state = "open"
        for bar in window:
            stopped = bar.low <= stop if sign > 0 else bar.high >= stop
            reached = bar.high >= target if sign > 0 else bar.low <= target
            if stopped:
                state = "stop"
                break
            if reached:
                state = "hit"
                break
        result["state"].append(state)
        result["r"].append(reward if state == "hit" else -1.0 if state == "stop" else last_close_r)
    result["tp1_first"] = result["state"][0] == "hit"
    result["stop_first"] = result["state"][0] == "stop"
    return result


# ---------------------------------------------------------------------- the replay

@dataclass
class Record:
    ts: datetime
    verdict: str
    quality: str
    alignment: str
    bias: str
    strength: float
    flags: List[str]
    blocker: str
    sizing: str
    directional: bool
    outcome: Dict[str, Any] = field(default_factory=dict)


def replay(
    builder: Any, broker: ReplayBroker, epic: str, *, step: int, horizon: int,
    min_strength: float, t: Any, warmup: int = 300, limit: Optional[int] = None,
    progress: Any = None,
) -> List[Record]:
    """Walk the M15 grid, rebuild the plan at each step, measure what followed."""
    grid = broker._histories.get("M15") or []
    records: List[Record] = []
    if len(grid) <= warmup + 1:
        return records
    indexes = list(range(warmup, len(grid) - 1, step))
    if limit:
        indexes = indexes[-limit:]
    for count, index in enumerate(indexes):
        moment = grid[index].ts
        broker.set_time(moment)
        try:
            plan = builder.build(epic, fundamentals=False)
        except Exception:
            continue
        entry_tf = (plan.style or {}).get("entry") or "H1"
        later = [c for c in broker._histories.get(entry_tf, []) if c.ts > moment]
        found = plan.assessment or {}
        records.append(Record(
            ts=moment, verdict=found.get("verdict", "-"), quality=found.get("quality", "-"),
            alignment=found.get("alignment", "-"), bias=plan.bias.value, strength=plan.confidence,
            flags=[f["key"] for f in found.get("flags", [])],
            blocker=funnel.auto_blocker(plan, min_strength=min_strength, t=t),
            sizing=(plan.sizing or {}).get("verdict", "-"),
            directional=not plan.advisory_only and plan.bias.direction is not None,
            outcome=outcome(plan.direction, plan.reference_price, plan.sl, plan.targets,
                            later, horizon),
        ))
        if progress:
            progress(count + 1, len(indexes))
    return records


# ---------------------------------------------------------------------- summaries

def group_stats(records: Sequence[Record]) -> Dict[str, Any]:
    measured = [r for r in records if r.outcome.get("r")]
    n = len(measured)
    out: Dict[str, Any] = {"n": n}
    if not n:
        return out
    first = sum(1 for r in measured if r.outcome["tp1_first"])
    stop = sum(1 for r in measured if r.outcome["stop_first"])
    out.update(
        tp1_first=first / n, stop_first=stop / n, enough=n >= MIN_GROUP,
        interval=wilson(first, n) if n >= MIN_GROUP else None,
        mean_r=[statistics.mean(r.outcome["r"][k] for r in measured) for k in range(3)],
    )
    return out


def summarise(records: Sequence[Record]) -> Dict[str, Any]:
    """Group the measured decisions every useful way."""
    directional = [r for r in records if r.directional]
    summary: Dict[str, Any] = {
        "all": group_stats(records), "directional": group_stats(directional),
        "neutral": group_stats([r for r in records if not r.directional]),
        "by_verdict": {}, "by_flag": {}, "by_strength": {}, "by_alignment": {},
        "by_blocker": {}, "records": len(records),
    }
    for verdict in sorted({r.verdict for r in directional}):
        summary["by_verdict"][verdict] = group_stats([r for r in directional if r.verdict == verdict])
    flags = sorted({f for r in directional for f in r.flags})
    for flag in flags:
        summary["by_flag"][flag] = {
            "with": group_stats([r for r in directional if flag in r.flags]),
            "without": group_stats([r for r in directional if flag not in r.flags]),
        }
    buckets = (("<40", lambda s: s < 40), ("40-60", lambda s: 40 <= s < 60), (">=60", lambda s: s >= 60))
    for label, test in buckets:
        summary["by_strength"][label] = group_stats([r for r in directional if test(r.strength)])
    for state in sorted({r.alignment for r in directional}):
        summary["by_alignment"][state] = group_stats([r for r in directional if r.alignment == state])
    for key in sorted({r.blocker for r in records}):
        summary["by_blocker"][key] = group_stats([r for r in records if r.blocker == key])
    return summary


def render(summary: Dict[str, Any], *, epic: str, step: int, horizon: int, equity: Any) -> str:
    """Plain-text tables for the command line."""
    pct = lambda v: f"{v * 100:5.1f}%"  # noqa: E731

    def row(label: str, g: Dict[str, Any]) -> str:
        if not g.get("n"):
            return f"  {label:<34s} n=0"
        tail = ""
        if g.get("interval"):
            tail = f"  ({g['interval'][0] * 100:.0f}-{g['interval'][1] * 100:.0f}%)"
        elif not g.get("enough"):
            tail = "  (too few to judge)"
        r1, r2, r3 = g["mean_r"]
        return (f"  {label:<34s} n={g['n']:<5d} TP1 first {pct(g['tp1_first'])}{tail:<14s} "
                f"stop first {pct(g['stop_first'])}   mean R at TP1/TP2/TP3 "
                f"{r1:+.2f}/{r2:+.2f}/{r3:+.2f}")

    lines = [
        f"AUDIT {epic}: {summary['records']} past decisions, every {step} M15 bars, "
        f"followed {horizon} bars; equity {'actual' if equity is None else equity}",
        "Plan levels followed on later candles; no spread on exit, no trailing/break-even, "
        "samples overlap. Past results, not a forecast.", "",
        "All decisions", row("all", summary["all"]), row("with a direction", summary["directional"]),
        row("no clear direction", summary["neutral"]), "", "By the manual verdict (directional)",
    ]
    lines += [row(k, g) for k, g in summary["by_verdict"].items()]
    lines += ["", "By signal strength (directional)"]
    lines += [row(k, g) for k, g in summary["by_strength"].items()]
    lines += ["", "By higher-timeframe alignment (directional)"]
    lines += [row(k, g) for k, g in summary["by_alignment"].items()]
    lines += ["", "Each quality warning: setups WITH it vs WITHOUT it (directional)"]
    for flag, pair in summary["by_flag"].items():
        lines += [row(f"{flag}: with", pair["with"]), row(f"{flag}: without", pair["without"])]
    lines += ["", "By what stops automatic mode (first failing check)"]
    lines += [row(k, g) for k, g in summary["by_blocker"].items()]
    lines += ["", "How to read it: a filter earns its place if setups it rejects reach TP1 clearly "
              "less often than the ones it lets through. Where the two rows look alike, the "
              "filter removes opportunities without improving quality."]
    return "\n".join(lines)
