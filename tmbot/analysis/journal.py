"""Performance journal.

Answers the only question that matters once the bot is running: *is this
working?*  Everything is measured in R -- what a trade returned as a multiple
of what it risked -- because R is scale-free.  A 0.1 lot and a 10 lot on the
same idea score identically, so the number reflects the quality of the
decisions rather than the size of the account.

Two things this deliberately does not do.  It does not report account currency,
because broker fees, overnight charges and slippage are not captured here and a
figure that looks like a P&L statement but is not one is worse than no figure.
And it does not claim the reversal failsafe "saved" or "cost" you anything --
that needs a counterfactual nobody has.  It reports the measurable comparison:
what trades with a reversal returned against those without.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from ..i18n import Translator
from ..models import Fill, utcnow
from ..store import Store

STAGES = ("TP1", "TP2", "TP3")


@dataclass
class TradeResult:
    deal_id: str
    epic: str
    total_r: float
    stages: List[str] = field(default_factory=list)
    inferred: bool = False
    had_reversal: bool = False

    @property
    def won(self) -> bool:
        return self.total_r > 0


@dataclass
class EpicStats:
    epic: str
    results: List[TradeResult] = field(default_factory=list)

    @property
    def trades(self) -> int:
        return len(self.results)

    @property
    def wins(self) -> int:
        return sum(1 for r in self.results if r.won)

    @property
    def losses(self) -> int:
        return self.trades - self.wins

    @property
    def total_r(self) -> float:
        return sum(r.total_r for r in self.results)

    @property
    def average_r(self) -> float:
        return self.total_r / self.trades if self.trades else 0.0

    def hits(self, stage: str) -> int:
        return sum(1 for r in self.results if stage in r.stages)


@dataclass
class Journal:
    days: int
    epics: List[EpicStats] = field(default_factory=list)
    inferred: int = 0
    with_reversal: List[TradeResult] = field(default_factory=list)
    without_reversal: List[TradeResult] = field(default_factory=list)

    @property
    def trades(self) -> int:
        return sum(stats.trades for stats in self.epics)

    @property
    def total_r(self) -> float:
        return sum(stats.total_r for stats in self.epics)

    @property
    def best(self) -> Optional[EpicStats]:
        return max(self.epics, key=lambda s: s.total_r) if self.epics else None

    @property
    def worst(self) -> Optional[EpicStats]:
        return min(self.epics, key=lambda s: s.total_r) if self.epics else None


def build(store: Store, days: int = 30) -> Journal:
    """Aggregate recorded fills into per-instrument performance."""
    since = utcnow() - timedelta(days=days)
    fills = store.fills_since(since)
    reversed_deals = {
        event["deal_id"] for event in store.events_since(since, kind="reversal")
    }

    by_deal: Dict[str, List[Fill]] = {}
    for fill in fills:
        by_deal.setdefault(fill.deal_id, []).append(fill)

    results: List[TradeResult] = []
    for deal_id, deal_fills in by_deal.items():
        results.append(TradeResult(
            deal_id=deal_id,
            epic=deal_fills[0].epic,
            total_r=round(sum(f.r_multiple for f in deal_fills), 4),
            stages=[f.stage for f in deal_fills],
            inferred=any(f.inferred for f in deal_fills),
            had_reversal=deal_id in reversed_deals,
        ))

    grouped: Dict[str, EpicStats] = {}
    for result in results:
        grouped.setdefault(result.epic, EpicStats(result.epic)).results.append(result)

    return Journal(
        days=days,
        epics=sorted(grouped.values(), key=lambda s: s.total_r, reverse=True),
        inferred=sum(1 for r in results if r.inferred),
        with_reversal=[r for r in results if r.had_reversal],
        without_reversal=[r for r in results if not r.had_reversal],
    )


def _average(results: List[TradeResult]) -> float:
    return sum(r.total_r for r in results) / len(results) if results else 0.0


def render(journal: Journal, t: Optional[Translator] = None) -> str:
    t = t or Translator()
    if not journal.trades:
        return t("journal.empty", days=journal.days)

    lines = [t("journal.heading", days=journal.days), ""]
    for stats in journal.epics:
        lines.append(t(
            "journal.epic", epic=stats.epic, trades=stats.trades,
            wins=stats.wins, losses=stats.losses,
            total=f"{stats.total_r:+.2f}", average=f"{stats.average_r:+.2f}",
        ))
        reached = [
            t("journal.stage", stage=stage, hits=stats.hits(stage),
              trades=stats.trades,
              percent=f"{stats.hits(stage) / stats.trades * 100:.0f}")
            for stage in STAGES
        ]
        lines.append("  " + "   ".join(reached))
        lines.append("")

    lines.append(t(
        "journal.total", trades=journal.trades, total=f"{journal.total_r:+.2f}",
        best=journal.best.epic if journal.best else "-",
        worst=journal.worst.epic if journal.worst else "-",
    ))

    if journal.with_reversal:
        lines.append(t(
            "journal.reversal",
            count=len(journal.with_reversal),
            with_avg=f"{_average(journal.with_reversal):+.2f}",
            without=len(journal.without_reversal),
            without_avg=f"{_average(journal.without_reversal):+.2f}",
        ))
    if journal.inferred:
        lines.append(t("journal.inferred", count=journal.inferred))
    return "\n".join(lines)
