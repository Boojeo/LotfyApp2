"""/afford and the skip list for automatic trading: which instruments fit this account.

Read-only.  It reads the broker's lot rules, the spread, the ATR and the account
equity, applies ``analysis.afford`` and reports.  It changes no rule: the risk
limit, the minimum lot, the stop and the risk % are exactly as they were.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from ..analysis import afford as afford_module
from ..analysis import style as style_module
from ..analysis.afford import Afford
from ..analysis.indicators import atr as atr_series
from ..analysis.indicators import last_value

log = logging.getLogger(__name__)

TTL = 300.0       # seconds an answer is reused: equity and ATR move slowly


class AffordService:
    def __init__(self, supervisor: Any):
        self.sup = supervisor
        self._cache: Dict[str, Tuple[float, Afford]] = {}

    @property
    def t(self):
        return self.sup.t

    # ------------------------------------------------------------------ computing

    def compute(self, epic: str, *, force: bool = False) -> Afford:
        cached = self._cache.get(epic)
        if cached and not force and time.monotonic() - cached[0] < TTL:
            return cached[1]
        result = self._compute(epic)
        self._cache[epic] = (time.monotonic(), result)
        return result

    def _compute(self, epic: str) -> Afford:
        config = self.sup.config
        risk = config.report.risk_percent
        try:
            equity = style_module.equity_of(self.sup.broker.account_summary())
        except Exception as exc:
            log.warning("afford: no equity (%s)", exc)
            equity = None
        try:
            rules = self.sup.broker.market_rules(epic)
            lot = rules.min_deal_size or rules.size_step
            value = rules.value_per_point
        except Exception as exc:
            log.warning("afford: no market rules for %s (%s)", epic, exc)
            lot, value = None, None
        try:
            spread = self.sup.broker.quote(epic).spread
        except Exception:
            spread = 0.0
        atr = None
        try:
            candles = self.sup._cached_candles(epic, config.analysis.entry_timeframe, 100)
            atr = last_value(atr_series(candles, config.analysis.atr_period))
        except Exception as exc:
            log.warning("afford: no ATR for %s (%s)", epic, exc)
        typical = None
        try:
            plan = self.sup.store.latest_plan(epic, max_age_hours=48)
            typical = plan.risk if plan else None
        except Exception:
            typical = None
        return afford_module.assess(
            epic=epic, equity=equity, risk_percent=risk, value_per_point=value, min_lot=lot,
            atr=atr, min_stop_atr=config.analysis.min_stop_atr, spread=spread,
            slippage=config.report.slippage, commission_per_lot=config.report.commission_per_lot,
            typical_stop=typical)

    def unaffordable(self, epic: str) -> Optional[Afford]:
        """The answer when this instrument is KNOWN not to fit; None otherwise.

        Unknown is never "unaffordable": with a missing value, lot or ATR nothing
        is skipped and the normal checks decide, as before.
        """
        try:
            result = self.compute(epic)
        except Exception:
            log.exception("afford check failed for %s", epic)
            return None
        return result if result.unaffordable else None

    # ------------------------------------------------------------------ /afford

    def command(self, argument: str) -> str:
        t = self.t
        names = ([self.sup.config.canonical_epic(argument)] if argument.strip()
                 else [item.epic for item in self.sup.config.analysis.watchlist])
        if not names:
            return t("afford.none")
        results = [self.compute(name, force=True) for name in names]
        first = results[0]
        if not first.equity:
            return t("afford.no_equity")
        lines = [t("afford.heading", equity=f"{first.equity:,.2f}",
                   risk=f"{first.risk_percent:g}", money=f"{first.max_money:,.2f}")]
        order = sorted(results, key=lambda r: (not r.affordable, r.unaffordable is False, r.epic))
        for r in order:
            lines.append(self._line(r))
        checks = [self._check_line(r) for r in order]
        checks = [c for c in checks if c]
        if checks:
            lines.append(t("afford.check_head"))
            lines += checks
        lines.append(t("afford.note"))
        return "\n".join(lines)

    # a cross-check against the broker's own calculator; informational only
    TOLERANCE = 0.01

    def _check_line(self, r: Afford) -> str:
        """Compare the bot's loss figure with the broker's own, when it has one."""
        if not r.known:
            return ""
        from ..models import Direction
        try:
            quote = self.sup.broker.quote(r.epic)
            entry = quote.mid
            theirs = self.sup.broker.calc_loss(
                r.epic, Direction.BUY, r.min_lot, entry, entry - r.tight_stop)
        except Exception:
            return ""
        if theirs is None:
            return ""
        ours = r.tight_stop * r.value * r.min_lot       # no spread, slippage or commission
        gap = abs(ours - theirs) / theirs if theirs else 0.0
        key = "afford.check_ok" if gap <= self.TOLERANCE else "afford.check_bad"
        return self.t(key, epic=r.epic, lot=f"{r.min_lot:g}", stop=f"{r.tight_stop:.5g}",
                      theirs=f"{theirs:,.4f}", ours=f"{ours:,.4f}", gap=f"{gap * 100:.2f}")

    def _line(self, r: Afford) -> str:
        t = self.t
        if not r.known:
            return t("afford.unknown", epic=r.epic, why=t(f"afford.why.{r.why_unknown}"))
        digits = 5 if r.tight_stop < 1 else 2
        fmt = lambda v: f"{v:.{digits}f}"  # noqa: E731
        typical = ""
        if r.typical_stop:
            typical = t("afford.typical", stop=fmt(r.typical_stop),
                        need=f"{r.min_equity_typical:,.0f}")
        if r.affordable:
            return t("afford.ok", epic=r.epic, lot=f"{r.min_lot:g}", max_stop=fmt(r.max_stop),
                     tight=fmt(r.tight_stop), n=f"{r.max_stop / r.atr:.1f}") + typical
        return t("afford.no", epic=r.epic, lot=f"{r.min_lot:g}", tight=fmt(r.tight_stop),
                 loss=f"{r.tight_loss:,.2f}", limit=f"{r.max_money:,.2f}",
                 need=f"{r.min_equity_tight:,.0f}", risk=f"{r.risk_percent:g}") + typical
