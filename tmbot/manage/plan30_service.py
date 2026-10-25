"""Telegram face of the 30-day plan: /plan30, the daily review, the loss-limit alert.

Read-only by design.  It reads fills and open positions, writes only its own
record in the database and sends messages; it never places or modifies a deal
and never touches the bot's settings.  The daily loss limit can only *warn* --
this bot does not open trades, so stopping is up to you.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as time_of_day, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..analysis import plan30 as plan_module
from ..analysis.plan30 import DayLog, Plan30
from ..models import utcnow

log = logging.getLogger(__name__)

STEP_SECONDS = 30.0


class Plan30Service:
    def __init__(self, supervisor: Any):
        self.sup = supervisor
        self._next_step = 0.0
        self._values: Dict[str, Optional[float]] = {}

    # ------------------------------------------------------------------ plumbing

    @property
    def t(self):
        return self.sup.t

    def _tz(self):
        return self.sup._report_timezone()

    def _today(self):
        return utcnow().astimezone(self._tz()).date()

    def _load(self) -> Optional[Plan30]:
        return Plan30.load(self.sup.store)

    def _value_of(self, epic: str) -> Optional[float]:
        if epic not in self._values:
            try:
                self._values[epic] = self.sup.broker.market_rules(epic).value_per_point
            except Exception as exc:
                log.warning("plan30: no contract value for %s (%s)", epic, exc)
                self._values[epic] = None
        return self._values[epic]

    def _fills(self, plan: Plan30):
        begin = datetime.combine(plan.start_date, time_of_day(0), tzinfo=self._tz())
        # a day early: store timestamps are UTC and local midnight can fall either side
        return self.sup.store.fills_since(begin.astimezone(timezone.utc) - timedelta(days=1))

    def _currency(self, plan: Plan30) -> str:
        return plan.currency

    def _fmt(self, value: Optional[float]) -> str:
        return "-" if value is None else f"{value:,.2f}"

    def _signed(self, value: Optional[float]) -> str:
        return "-" if value is None else f"{value:+,.2f}"

    # ------------------------------------------------------------------ schedule

    def step(self) -> None:
        """Called every tick; does its work at most every half minute."""
        now = time.monotonic()
        if now < self._next_step:
            return
        self._next_step = now + STEP_SECONDS
        plan = self._load()
        if plan is None:
            return
        try:
            self._finalize_due(plan)
            self._watch_loss_limit(plan)
        except Exception:
            log.exception("plan30 step failed")

    def _finalize_due(self, plan: Plan30) -> None:
        """Review every finished day, in order, and never rewrite a reviewed one."""
        today_number = plan.day_number(self._today())
        changed = False
        fills = None
        for number in range(1, min(today_number - 1, plan.days) + 1):
            day = plan.day(number)
            if day.final:
                continue
            start = plan_module.rows(plan)[number - 1].actual_start
            if start is None:
                break
            if day.manual and day.net is not None:
                self._check_manual(plan, day, start)
            else:
                fills = self._fills(plan) if fills is None else fills
                found = plan_module.evaluate_day(
                    fills, plan, number, start, self._tz(), self._value_of)
                if found is None:
                    if number not in plan.pending_alerted:
                        plan.pending_alerted.append(number)
                        self.sup.notifier.send(
                            self.t("p30.pending", n=number, date=plan.date_of(number)),
                            level="warn")
                        changed = True
                    break
                found.note = day.note or found.note
                for key, value in found.to_dict().items():
                    setattr(day, key, value)
            day.decision = plan_module.decide(plan, number)
            if not day.trades and day.net == 0 and not day.note:
                day.note = self.t("p30.no_trade_note")
            day.final = True
            changed = True
            self.sup.notifier.send(self.review_text(plan, number))
        if (today_number > plan.days and not plan.final_sent
                and all(plan.day(n).final for n in range(1, plan.days + 1))):
            plan.final_sent = True
            changed = True
            self.sup.notifier.send(self.final_text(plan))
        if changed:
            plan.save(self.sup.store)

    def _check_manual(self, plan: Plan30, day: DayLog, start: float) -> None:
        day.loss_limit_hit = day.net <= -plan_module.daily_limit(plan, start)
        day.broken = [plan_module.RULE_LOSS_LIMIT] if day.net < -plan_module.daily_limit(plan, start) else []

    def _watch_loss_limit(self, plan: Plan30) -> None:
        number = plan.day_number(self._today())
        if not 1 <= number <= plan.days or number in plan.limit_alerted:
            return
        row = plan_module.rows(plan)[number - 1]
        if row.actual_start is None or plan.day(number).final:
            return
        net = self._today_net(plan, number, row.actual_start, live=True)[0]
        limit = plan_module.daily_limit(plan, row.actual_start)
        if net is not None and net <= -limit:
            plan.limit_alerted.append(number)
            plan.save(self.sup.store)
            self.sup.notifier.send(self.t(
                "p30.limit_hit", n=number, net=self._signed(net), limit=self._fmt(limit),
                percent=f"{plan.loss_limit * 100:g}", currency=plan.currency), level="warn")

    def _today_net(self, plan: Plan30, number: int, start: float, *, live: bool):
        """(net so far, estimated?, trades) for ``number``; open positions count when ``live``."""
        day = plan.day(number)
        if day.manual and day.net is not None:
            realized, estimated, trades = day.net, False, day.trades
        else:
            found = plan_module.evaluate_day(
                self._fills(plan), plan, number, start, self._tz(), self._value_of)
            if found is None:
                return None, False, 0
            realized, estimated, trades = found.net or 0.0, found.estimated, found.trades
        unrealized = 0.0
        if live:
            try:
                unrealized = sum(p.upl for p in self.sup.broker.positions()
                                 if getattr(p, "upl", None) is not None)
            except Exception:
                unrealized = 0.0
        return plan_module.money(realized + unrealized), estimated, trades

    # ------------------------------------------------------------------ commands

    def command(self, argument: str) -> str:
        parts = argument.strip().split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        handlers = {
            "": self.status, "start": self.start, "table": self.table,
            "result": self.result, "note": self.note, "export": self.export,
            "report": self.report,
        }
        handler = handlers.get(sub)
        if handler is None:
            return self.t("p30.usage")
        return handler(rest) if sub not in ("", "table", "export", "report") else handler()

    def start(self, argument: str = "") -> str:
        existing = self._load()
        if existing is not None:
            return self.t("p30.exists", date=existing.start_date)
        capital = plan_module.DEFAULT_CAPITAL
        if argument.strip():
            try:
                capital = float(argument.split()[0])
            except ValueError:
                return self.t("p30.usage")
            if capital <= 0:
                return self.t("p30.usage")
        try:
            currency = self.sup.broker.account_summary().get("currency") or "USD"
        except Exception:
            currency = "USD"
        plan = Plan30(start_date=self._today(), capital=plan_module.money(capital),
                      risk=self.sup.config.report.risk_percent / 100.0 or plan_module.DEFAULT_RISK,
                      currency=currency)
        plan.save(self.sup.store)
        return self.t("p30.started", capital=self._fmt(plan.capital), currency=currency,
                      date=plan.start_date, percent=f"{plan.loss_limit * 100:g}",
                      risk=f"{plan.risk * 100:g}")

    def status(self) -> str:
        plan = self._load()
        if plan is None:
            return self.t("p30.not_started")
        number = plan.day_number(self._today())
        if number > plan.days:
            return self.final_text(plan)
        table = plan_module.rows(plan)
        row = table[number - 1]
        net, estimated, trades = None, False, 0
        if row.actual_start is not None:
            net, estimated, trades = self._today_net(
                plan, number, row.actual_start, live=False)
        limit_line = ""
        if row.actual_start is None or net is None:
            today_line = self.t("p30.today_unknown")
        else:
            limit = plan_module.daily_limit(plan, row.actual_start)
            used = max(0.0, -net) / limit * 100 if limit else 0.0
            today_line = (self.t("p30.today_none") if not trades and net == 0 else
                          self.t("p30.today", net=self._signed(net), trades=trades or "-",
                                 estimated=self.t("p30.estimated") if estimated else ""))
            limit_line = "\n" + self.t(
                "p30.limit", limit=self._fmt(limit), currency=plan.currency,
                used=f"{used:.0f}")
        final = plan_module.actual_balance(plan)
        figures = plan_module.summary(plan)
        return self.t(
            "p30.status", n=number, days=plan.days, date=plan.date_of(number),
            actual=self._fmt(final), theory=self._fmt(row.theoretical_end),
            currency=plan.currency, today=today_line, limit=limit_line,
            net=self._signed(figures["net"]), pct=f"{figures['return_pct']:+.2f}",
            gap=self._fmt(plan_module.money(row.theoretical_end - final)),
        ) + self._pending_line(plan)

    def _pending_line(self, plan: Plan30) -> str:
        waiting = [n for n in plan.pending_alerted if not plan.day(n).final]
        return ("\n" + self.t("p30.pending_short", days=", ".join(map(str, waiting)))) if waiting else ""

    def table(self) -> str:
        plan = self._load()
        if plan is None:
            return self.t("p30.not_started")
        number = min(plan.day_number(self._today()), plan.days)
        lines = [self.t("p30.table_head", currency=plan.currency)]
        for row in plan_module.rows(plan)[:max(number, 1)]:
            mark = "-" if row.achieved is None else ("✅" if row.achieved else "❌")
            lines.append(self.t(
                "p30.table_row", n=row.number, date=f"{row.day:%m-%d}",
                a_start=self._fmt(row.actual_start), a_end=self._fmt(row.actual_end),
                net=self._signed(row.net), t_start=self._fmt(row.theoretical_start),
                t_end=self._fmt(row.theoretical_end), mark=mark, trades=row.trades or "-"))
        lines.append(self.t("p30.table_foot"))
        return "\n".join(lines)

    def result(self, argument: str) -> str:
        plan = self._load()
        if plan is None:
            return self.t("p30.not_started")
        tokens = argument.split()
        number = plan.day_number(self._today())
        try:
            if len(tokens) >= 2 and tokens[0].isdigit():
                float(tokens[1])
                number, tokens = int(tokens[0]), tokens[1:]
            amount = plan_module.money(float(tokens[0]))
        except (ValueError, IndexError):
            return self.t("p30.usage")
        note = " ".join(tokens[1:])
        if not 1 <= number <= plan.days or number > plan.day_number(self._today()):
            return self.t("p30.bad_day", days=plan.days)
        day = plan.day(number)
        if day.final:
            return self.t("p30.locked", n=number)
        day.net, day.manual = amount, True
        if note:
            day.note = note
        plan.save(self.sup.store)
        self._next_step = 0.0
        if number < plan.day_number(self._today()):
            self.step()
        return self.t("p30.recorded", n=number, net=self._signed(amount), currency=plan.currency)

    def note(self, argument: str) -> str:
        plan = self._load()
        if plan is None:
            return self.t("p30.not_started")
        if not argument.strip():
            return self.t("p30.usage")
        number = plan.day_number(self._today())
        if not 1 <= number <= plan.days:
            return self.t("p30.bad_day", days=plan.days)
        day = plan.day(number)
        if day.final:
            return self.t("p30.locked", n=number)
        day.note = argument.strip()
        plan.save(self.sup.store)
        return self.t("p30.noted", n=number)

    def export(self) -> str:
        plan = self._load()
        if plan is None:
            return self.t("p30.not_started")
        try:
            from ..analysis import plan30_xlsx
        except ImportError:  # pragma: no cover
            return self.t("p30.no_excel")
        directory = Path(self.sup.config.resolved_report_dir)
        path = directory / f"plan30-{self.sup.config.broker.environment}.xlsx"
        try:
            self._refresh_live(plan)
            plan30_xlsx.export(plan, path, decision_text=lambda key: self.t(f"p30.decision.{key}"))
        except ImportError:
            return self.t("p30.no_excel")
        except OSError as exc:
            return self.t("p30.export_failed", error=exc)
        self.sup.notifier.send_document(str(path), self.t("p30.exported", path=path))
        return ""

    def _refresh_live(self, plan: Plan30) -> None:
        """Today's row is shown as it stands; it is not final until the day ends."""
        number = plan.day_number(self._today())
        if not 1 <= number <= plan.days or plan.day(number).final:
            return
        row = plan_module.rows(plan)[number - 1]
        if row.actual_start is None:
            return
        day = plan.day(number)
        if day.manual and day.net is not None:
            return
        found = plan_module.evaluate_day(
            self._fills(plan), plan, number, row.actual_start, self._tz(), self._value_of)
        if found is not None:
            note = day.note or self.t("p30.in_progress")
            for key, value in found.to_dict().items():
                setattr(day, key, value)
            day.note, day.final = note, False

    def report(self) -> str:
        plan = self._load()
        return self.final_text(plan) if plan else self.t("p30.not_started")

    # ------------------------------------------------------------------ messages

    def review_text(self, plan: Plan30, number: int) -> str:
        row = plan_module.rows(plan)[number - 1]
        log = row.log
        if not log.trades and log.net == 0:
            body = self.t("p30.review_no_trade")
        else:
            body = self.t(
                "p30.review_trades", trades=log.trades, wins=log.wins, losses=log.losses,
                costs=self._fmt(log.costs),
                dd="-" if log.max_dd is None else f"{log.max_dd:.2f}%")
        pct = (row.net / row.actual_start * 100.0) if row.actual_start else 0.0
        rules = (self.t("p30.rules_ok") if not log.broken else self.t(
            "p30.rules_broken",
            rules=self.t.semicolon.join(self.t(f"p30.rule.{k}") for k in log.broken)))
        return self.t(
            "p30.review", n=number, date=row.day, start=self._fmt(row.actual_start),
            end=self._fmt(row.actual_end), currency=plan.currency, pct=f"{pct:+.2f}",
            achieved=self.t("p30.yes") if row.achieved else self.t("p30.no"),
            body=body, theory=self._fmt(row.theoretical_end),
            gap=self._fmt(plan_module.money(row.theoretical_end - (row.actual_end or 0))),
            rules=rules, decision=self.t(f"p30.decision.{log.decision}"),
        ) + ("\n" + self.t("p30.estimated_note") if log.estimated else "")

    def final_text(self, plan: Plan30) -> str:
        s = plan_module.summary(plan)
        done = s["days_recorded"] >= plan.days
        return self.t(
            "p30.final", days=s["days_recorded"], total=plan.days,
            initial=self._fmt(s["initial"]), final=self._fmt(s["final"]),
            net=self._signed(s["net"]), pct=f"{s['return_pct']:+.2f}",
            theory=self._fmt(s["theoretical_final"]), diff=self._fmt(s["difference"]),
            wins=s["wins"], losses=s["losses"],
            rate="-" if s["win_rate"] is None else f"{s['win_rate']:.1f}%",
            dd="-" if s["max_drawdown"] is None else f"{s['max_drawdown']:.2f}%",
            costs=self._fmt(s["costs"]), currency=plan.currency,
            compliant=s["days_compliant"], reviewed=s["days_reviewed"],
            over=s["days_over_limit"],
            state=self.t("p30.done" if done else "p30.in_progress_state"),
        )
