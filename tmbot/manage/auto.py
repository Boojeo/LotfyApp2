"""Automatic trading mode, with every guard in front of the order.

Off unless THREE things are true: ``auto.enabled`` in the config, a real
``/auto_on``, and -- for a real-money account -- ``auto.allow_live`` plus a
one-time code typed back in Telegram.  Anything short of that places nothing.

What it does, in order, for one instrument per scan:

  cheap checks   permitted instrument, trading session, daily loss limit,
                 open-position cap, one position per instrument, cool-down,
                 no unresolved order      (nothing is analysed yet)
  the analysis   the same plan the reports use: technicals, news, quality,
                 risk sizing              (a failure here is logged, not traded)
  signal gates   verdict APPROVED, signal strength, higher timeframe aligned,
                 a clear direction, no "unsure or risky" reason, sized inside
                 the risk limit
  the order      one deal per leg with its own stop and target, attached at
                 the broker.  The stop is the plan's technical stop: it is
                 never widened to fit a lot size, and the risk % is never
                 raised to make a trade pass.

Duplicate protection writes its markers BEFORE the order is sent, so a crash
or a timeout can only ever block a repeat, never allow one.

``/auto_off`` stops new entries at once.  Open positions keep being managed by
the normal engine (stops, targets, trailing, reversal alerts) exactly as before;
nothing is closed unless you send ``/panic`` and confirm it.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as time_of_day, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..analysis import plan30 as plan30_module
from ..analysis import report as report_module
from ..analysis import style as style_module
from ..config import RISK_CHOICES
from ..models import Direction, ManagedTrade, TradePlan, TradeStatus, utcnow

log = logging.getLogger(__name__)

STATE_KEY = "auto_state"            # "on" | "off"
DAY_KEY = "auto_day"                # {"date": ..., "start": equity at the day's first look}
PENDING_KEY = "auto_pending"        # an order sent and not yet reconciled
WINDOW_KEY = "auto_window:"         # + epic: a basket we opened, to adopt it without asking
LAST_KEY = "auto_last:"             # + epic:side: when we last tried to enter
LIMIT_ALERT_KEY = "auto_limit_alert"
NOTIFIED_KEY = "auto_notified:"     # + epic:reason: rejection notices are throttled
ERROR_KEY = "auto_error_notified:"
ADOPT_WINDOW_SECONDS = 300
NOTICE_GAP = timedelta(hours=1)

# Rejections worth a message: the signal was good and something stopped it.
NOTIFY_REASONS = {"daily_limit", "positions_full", "risk_rejected", "risk_unchecked",
                  "order_failed", "equity_unknown"}


@dataclass
class Verdict:
    """Why a signal was or was not traded, in a form that can be logged and shown."""

    accepted: bool
    epic: str
    reason: str = ""                     # key under auto.reject.*
    args: Dict[str, Any] = field(default_factory=dict)
    plan: Optional[TradePlan] = None


class AutoTrader:
    def __init__(self, supervisor: Any):
        self.sup = supervisor
        self._lock = threading.Lock()
        self._next_scan = 0.0
        self._cursor = 0
        self._codes: Dict[str, Tuple[str, float]] = {}

    # ------------------------------------------------------------------ plumbing

    @property
    def t(self):
        return self.sup.t

    @property
    def cfg(self):
        return self.sup.config.auto

    @property
    def store(self):
        return self.sup.store

    def _now(self) -> datetime:
        return utcnow()

    def _tz(self):
        return self.sup._report_timezone()

    def environment(self) -> str:
        return self.sup.config.broker.environment

    def is_on(self) -> bool:
        return self.store.get(STATE_KEY) == "on"

    def instruments(self) -> List[str]:
        names = self.cfg.instruments or [item.epic for item in self.sup.config.analysis.watchlist]
        return [self.sup.config.canonical_epic(name) for name in names]

    def risk_percent(self) -> float:
        return self.sup.config.report.risk_percent

    def _event(self, kind: str, message: str, *, epic: Optional[str] = None,
               deal_id: Optional[str] = None) -> None:
        self.store.log_event(kind, message, epic=epic, deal_id=deal_id)
        log.info("%s %s %s", kind, epic or "", message)

    def _equity(self) -> Optional[float]:
        try:
            return style_module.equity_of(self.sup.broker.account_summary())
        except Exception as exc:
            log.warning("auto: could not read the account (%s)", exc)
            return None

    # ------------------------------------------------------------------ start-up

    def restore(self) -> None:
        """Run once when the bot starts.

        A real-money account always starts OFF: switching automatic trading on
        for live money is a decision made each session, never inherited from
        the last one.
        """
        if self.is_on() and self.environment() == "live":
            self.store.set(STATE_KEY, "off")
            self._event("auto_state", "forced OFF at start-up: live accounts never resume automatically")
            self.sup.notifier.send(self.t("auto.forced_off"), level="warn")
        elif self.is_on() and not self.cfg.enabled:
            self.store.set(STATE_KEY, "off")
            self._event("auto_state", "forced OFF at start-up: auto.enabled is false in the config")
        self.reconcile_pending()

    def reconcile_pending(self) -> None:
        """Settle an order that was in flight when the bot stopped."""
        raw = self.store.get(PENDING_KEY)
        if not raw:
            return
        try:
            pending = json.loads(raw)
        except ValueError:
            self.store.set(PENDING_KEY, "")
            return
        try:
            held = [p for p in self.sup.broker.positions() if p.epic == pending["epic"]]
        except Exception as exc:
            log.warning("auto: cannot settle the pending order yet (%s)", exc)
            return
        self.store.set(PENDING_KEY, "")
        self._event("auto_order", f"pending order on {pending['epic']} settled: "
                    f"{len(held)} position(s) now open", epic=pending["epic"])
        if held:
            self.sup.notifier.send(self.t("auto.pending_settled", epic=pending["epic"],
                                          count=len(held)), level="warn")

    # ------------------------------------------------------------------ on / off

    def _code_for(self, purpose: str) -> str:
        code = f"{secrets.randbelow(9000) + 1000}"
        self._codes[purpose] = (code, time.monotonic() + self.cfg.confirm_seconds)
        return code

    def _code_ok(self, purpose: str, given: str) -> bool:
        entry = self._codes.get(purpose)
        if not entry or not given:
            return False
        code, expires = entry
        if time.monotonic() > expires:
            self._codes.pop(purpose, None)
            return False
        if secrets.compare_digest(code, given.strip()):
            self._codes.pop(purpose, None)
            return True
        return False

    def on_command(self, argument: str) -> str:
        if self.is_on():
            return self.t("auto.already_on")
        problem = self._cannot_start()
        if problem:
            return problem
        if self.environment() == "live":
            if not self._code_ok("live_on", argument):
                code = self._code_for("live_on")
                return self.t("auto.live_confirm", code=code, seconds=f"{self.cfg.confirm_seconds:g}")
        self.store.set(STATE_KEY, "on")
        self._next_scan = 0.0
        self._event("auto_state", f"turned ON by user ({self.environment()}, "
                    f"risk {self.risk_percent():g}%)")
        return self.t("auto.on", env=self.environment().upper(),
                      risk=f"{self.risk_percent():g}", limit=f"{self.cfg.daily_loss_limit_percent:g}",
                      cap=self.cfg.max_open_positions, instruments=", ".join(self.instruments()),
                      start=self.cfg.session_start, end=self.cfg.session_end,
                      dry=self.t("auto.dry_run") if self.sup.config.dry_run else "")

    def _cannot_start(self) -> str:
        if not self.cfg.enabled:
            return self.t("auto.not_enabled")
        if self.environment() == "live" and not self.cfg.allow_live:
            return self.t("auto.live_not_allowed")
        if self.risk_percent() not in RISK_CHOICES:
            return self.t("auto.bad_risk", risk=f"{self.risk_percent():g}")
        if not self.instruments():
            return self.t("auto.no_instruments")
        try:
            if self.sup.broker.algo_trading_enabled() is False:
                return self.t("auto.algo_off")
            if (self.sup.config.management.exit_model == "three_deals"
                    and self.sup.broker.hedging_mode() is False):
                return self.t("auto.no_hedging")
        except Exception as exc:
            return self.t("auto.broker_unreadable", error=exc)
        return ""

    def off_command(self, argument: str = "") -> str:
        was_on = self.is_on()
        self.store.set(STATE_KEY, "off")
        self._event("auto_state", "turned OFF by user")
        return self.t("auto.off", was=self.t("auto.was_on") if was_on else self.t("auto.was_off"))

    def panic_command(self, argument: str) -> str:
        if not self._code_ok("panic", argument):
            code = self._code_for("panic")
            return self.t("auto.panic_confirm", code=code, seconds=f"{self.cfg.confirm_seconds:g}")
        self.store.set(STATE_KEY, "off")
        trades = [trade for trade in self.store.active_trades()]
        self._event("auto_state", f"PANIC: automatic trading OFF, closing {len(trades)} managed trade(s)")
        lines = [self.t("auto.panic_start", count=len(trades))]
        for trade in trades:
            try:
                lines.append(self.sup.close_command(trade.short_id))
            except Exception as exc:
                self._event("auto_error", f"panic close failed for {trade.short_id}: {exc}",
                            epic=trade.epic, deal_id=trade.deal_id)
                lines.append(self.t("auto.panic_failed", id=trade.short_id, error=exc))
        return "\n".join(lines)

    def legacy_command(self, argument: str) -> str:
        """``/auto`` used to mean something else; make a stray tap harmless."""
        return self.t("auto.legacy")

    # ------------------------------------------------------------------ risk setting

    def risk_command(self, argument: str) -> str:
        argument = argument.strip().rstrip("%")
        config = self.sup.config.report
        if not argument:
            return self._risk_text()
        try:
            value = float(argument)
        except ValueError:
            return self.t("risk.cmd_usage")
        if value not in RISK_CHOICES:
            return self.t("risk.cmd_usage")
        config.risk_percent = value
        self.store.set("risk_percent", f"{value:g}")
        self._event("risk", f"risk per trade set to {value:g}% by user")
        return self.t("risk.cmd_set", risk=f"{value:g}") + (
            "\n" + self.t("risk.cmd_higher", risk=f"{value:g}") if value > RISK_CHOICES[0] else "") \
            + "\n" + self._risk_text()

    def load_risk_override(self) -> None:
        raw = self.store.get("risk_percent")
        try:
            value = float(raw) if raw else None
        except ValueError:
            value = None
        if value in RISK_CHOICES:
            self.sup.config.report.risk_percent = value

    def _risk_text(self) -> str:
        equity = self._equity()
        value = self.risk_percent()
        money = f"{equity * value / 100:,.2f}" if equity else "-"
        return self.t("risk.cmd_status", risk=f"{value:g}", money=money,
                      equity=f"{equity:,.2f}" if equity else "-",
                      commission=f"{self.sup.config.report.commission_per_lot:g}",
                      slippage=f"{self.sup.config.report.slippage:g}")

    # ------------------------------------------------------------------ day state

    def day_state(self) -> Optional[Tuple[str, float, float]]:
        """(date, equity at the day's start, equity now), or None if unreadable."""
        equity = self._equity()
        if equity is None or equity <= 0:
            return None
        today = self._now().astimezone(self._tz()).date().isoformat()
        raw = self.store.get(DAY_KEY)
        try:
            saved = json.loads(raw) if raw else {}
        except ValueError:
            saved = {}
        if saved.get("date") != today:
            saved = {"date": today, "start": equity}
            self.store.set(DAY_KEY, json.dumps(saved))
        return today, float(saved["start"]), equity

    def daily_loss(self) -> Optional[Tuple[float, float, float]]:
        """(loss in money, loss as % of the day's start, limit in money); loss >= 0."""
        state = self.day_state()
        if state is None:
            return None
        _, start, equity = state
        loss = max(0.0, start - equity)
        return loss, loss / start * 100.0, start * self.cfg.daily_loss_limit_percent / 100.0

    # ------------------------------------------------------------------ the scan

    def step(self) -> None:
        """Called every tick; looks at one instrument every ``scan_minutes``."""
        if not self.is_on():
            return
        now = time.monotonic()
        if now < self._next_scan:
            return
        self._next_scan = now + self.cfg.scan_minutes * 60.0
        if getattr(self.sup, "_degraded", False):
            log.info("auto: broker connection degraded; not scanning")
            return
        names = self.instruments()
        if not names:
            return
        epic = names[self._cursor % len(names)]
        self._cursor += 1
        try:
            self.reconcile_pending()
            self.scan(epic)
        except Exception as exc:  # nothing in here may stop trade management
            log.exception("auto: scan of %s failed", epic)
            self._error(epic, f"scan failed: {exc}")

    def scan(self, epic: str) -> Verdict:
        verdict = self.evaluate(epic)
        if not verdict.accepted:
            self._rejected(verdict)
            return verdict
        return self.place(verdict.plan)

    def evaluate(self, epic: str) -> Verdict:
        """Every reason not to trade ``epic`` right now, cheapest first."""
        def no(reason: str, **args: Any) -> Verdict:
            return Verdict(False, epic, reason, args)

        if epic not in self.instruments():
            return no("not_permitted")
        if self._pending_blocks():
            return no("order_pending")      # safety first: an unsettled order blocks all
        if not self.in_session():
            return no("outside_session", start=self.cfg.session_start, end=self.cfg.session_end)

        loss = self.daily_loss()
        if loss is None:
            return no("equity_unknown")
        if loss[1] >= self.cfg.daily_loss_limit_percent - 1e-9:
            return no("daily_limit", loss=f"{loss[0]:,.2f}", percent=f"{loss[1]:.2f}",
                      limit=f"{self.cfg.daily_loss_limit_percent:g}")

        try:
            positions = self.sup.broker.positions()
        except Exception as exc:
            return no("data_error", error=exc)
        if any(p.epic == epic for p in positions):
            return no("open_on_instrument")
        if len(positions) >= self.cfg.max_open_positions:
            return no("positions_full", count=len(positions), cap=self.cfg.max_open_positions)

        try:
            plan = self.sup._plan_for(epic, force=True)
        except Exception as exc:
            return no("no_plan", error=exc)

        return self._judge(plan, len(positions))

    def _judge(self, plan: TradePlan, open_now: int) -> Verdict:
        epic = plan.epic

        def no(reason: str, **args: Any) -> Verdict:
            return Verdict(False, epic, reason, args, plan)

        found = plan.assessment or {}
        if plan.advisory_only or plan.bias.direction is None:
            return no("no_direction")
        if plan.bias.direction is not plan.direction:
            return no("against_bias")
        if found.get("verdict") != "APPROVED":
            return no("not_approved", verdict=found.get("verdict", "-"))
        if plan.confidence < self.cfg.min_signal_strength:
            return no("weak_signal", strength=f"{plan.confidence:.0f}",
                      minimum=f"{self.cfg.min_signal_strength:g}")
        if found.get("alignment") != "ALIGNED":
            return no("not_aligned", alignment=found.get("alignment", "-"))
        sizing = plan.sizing or {}
        if not sizing or sizing.get("verdict") == "UNCHECKED":
            return no("risk_unchecked")
        if sizing.get("verdict") not in ("OK", "REDUCED_LEGS") or not sizing.get("legs"):
            return no("risk_rejected", percent=f"{sizing.get('risk_percent', 0):g}",
                      minimum=f"{sizing.get('min_lot_money', 0):,.2f}",
                      money=f"{sizing.get('max_money', 0):,.2f}",
                      currency=sizing.get("currency", ""))
        doubts = report_module.detail_reasons(plan, self.t)
        if doubts:
            return no("doubtful", reasons=self.t.semicolon.join(doubts))

        if sizing.get("risk_percent") != self.risk_percent():
            return no("risk_mismatch")
        if sizing["legs"] + open_now > self.cfg.max_open_positions:
            return no("positions_full", count=open_now, cap=self.cfg.max_open_positions)
        try:
            rules = self.sup.broker.market_rules(epic)
        except Exception as exc:
            return no("data_error", error=exc)
        if not rules.tradeable:
            return no("not_tradeable")
        if self._cooling_down(epic, plan.direction):
            return no("cooldown", hours=f"{self.cfg.cooldown_hours:g}")
        return Verdict(True, epic, plan=plan)

    # ------------------------------------------------------------------ individual guards

    def in_session(self, when: Optional[datetime] = None) -> bool:
        """True inside the configured UTC trading hours on a permitted weekday."""
        when = (when or self._now()).astimezone(timezone.utc)
        if when.weekday() not in self.cfg.weekdays:
            return False
        start = time_of_day(*(int(x) for x in self.cfg.session_start.split(":")))
        end = time_of_day(*(int(x) for x in self.cfg.session_end.split(":")))
        now = when.time().replace(second=0, microsecond=0)
        return start <= now < end if start <= end else (now >= start or now < end)

    def _pending_blocks(self) -> bool:
        """An order whose outcome is not settled blocks every new one.

        Cleared by ``reconcile_pending`` once the broker's positions can be read;
        an unreadable marker blocks too -- a repeat order is the one mistake
        this mode must not make.
        """
        return bool(self.store.get(PENDING_KEY))

    def _cooling_down(self, epic: str, direction: Direction) -> bool:
        raw = self.store.get(f"{LAST_KEY}{epic}:{direction.value}")
        if not raw:
            return False
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return True
        return self._now() - last < timedelta(hours=self.cfg.cooldown_hours)

    # ------------------------------------------------------------------ the order

    def place(self, plan: TradePlan) -> Verdict:
        """Send the deals for ``plan``.  Markers are written before anything is sent."""
        epic, direction, sizing = plan.epic, plan.direction, plan.sizing
        legs, per_leg = int(sizing["legs"]), float(sizing["per_leg"])
        targets = [plan.targets[k - 1] for k in report_module.DEAL_TARGETS.get(legs, (1,))]
        with self._lock:
            now = self._now()
            if self.sup.config.dry_run:
                self._event("auto_order", f"[dry-run] would open {direction.value} {legs} x "
                            f"{per_leg} {epic} SL {plan.sl}", epic=epic)
                self.sup.notifier.send(self.t("auto.dry_opened", epic=epic,
                                              side=self.t.term("deal", direction.value),
                                              legs=legs, lots=per_leg, sl=plan.sl))
                self.store.set(f"{LAST_KEY}{epic}:{direction.value}", now.isoformat())
                return Verdict(True, epic, plan=plan)
            # Markers first: whatever happens next, a repeat is blocked.
            self.store.set(PENDING_KEY, json.dumps({"epic": epic, "ts": now.isoformat()}))
            self.store.set(f"{LAST_KEY}{epic}:{direction.value}", now.isoformat())
            self.store.set(f"{WINDOW_KEY}{epic}", json.dumps({
                "direction": direction.value,
                "until": (now + timedelta(seconds=ADOPT_WINDOW_SECONDS)).isoformat()}))
            try:
                before = {p.deal_id for p in self.sup.broker.positions()}
            except Exception:
                before = set()
            rules = self.sup.broker.market_rules(epic)
            stop = rules.round_price(plan.sl)
            sent, failure = 0, None
            for index, target in enumerate(targets):
                self._event("auto_order", f"request {index + 1}/{legs}: {direction.value} "
                            f"{per_leg} {epic} SL {stop} TP {rules.round_price(target)}", epic=epic)
                try:
                    reference = self.sup.broker.open_position(
                        epic, direction, per_leg, stop_level=stop,
                        profit_level=rules.round_price(target))
                    self._event("auto_order", f"deal {index + 1}/{legs} accepted "
                                f"(ref {reference})", epic=epic)
                    sent += 1
                except Exception as exc:
                    failure = exc
                    self._event("auto_error", f"deal {index + 1}/{legs} failed: {exc}", epic=epic)
                    break        # never push on after a failure: no retries, no repeats
            opened = self._settle(epic, direction, before)
            if opened == 0:
                # Nothing opened: close the adopt-without-asking window so a deal
                # you place by hand in the next minutes is never taken for ours.
                self.store.set(f"{WINDOW_KEY}{epic}", "")
        if failure is not None:
            verdict = Verdict(False, epic, "order_failed", {
                "opened": max(opened, 0), "legs": legs, "error": failure}, plan)
            self._rejected(verdict)
            return verdict
        self._event("auto_open", f"{direction.value} {legs} x {per_leg} {epic} opened, "
                    f"SL {stop}", epic=epic)
        self.sup.notifier.send(self._opened_text(plan, legs, per_leg))
        return Verdict(True, epic, plan=plan)

    def _settle(self, epic: str, direction: Direction, before: set) -> int:
        """How many new deals really exist on the broker; clears the pending marker."""
        try:
            now_open = [p for p in self.sup.broker.positions()
                        if p.epic == epic and p.direction is direction and p.deal_id not in before]
        except Exception as exc:
            log.warning("auto: could not confirm what opened on %s (%s); the marker stays", epic, exc)
            return -1
        self.store.set(PENDING_KEY, "")
        return len(now_open)

    def owns(self, position: Any) -> bool:
        """True for a position our own order just created (adopted without asking)."""
        raw = self.store.get(f"{WINDOW_KEY}{position.epic}")
        if not raw:
            return False
        try:
            window = json.loads(raw)
            until = datetime.fromisoformat(window["until"])
        except (ValueError, KeyError):
            return False
        return (window.get("direction") == position.direction.value
                and self._now() <= until)

    def adopt(self, trade: ManagedTrade) -> str:
        """Start managing one of our own deals with no /confirm."""
        self._event("auto_adopt", "adopted automatically", epic=trade.epic, deal_id=trade.deal_id)
        return self.sup.confirm(trade.short_id)

    # ------------------------------------------------------------------ messages

    def _opened_text(self, plan: TradePlan, legs: int, per_leg: float) -> str:
        s = plan.sizing
        digits = max(report_module._digits(plan.reference_price), 2)
        return self.t(
            "auto.opened", epic=plan.epic, side=self.t.term("deal", plan.direction.value),
            legs=legs, lots=per_leg, entry=plan.reference_price, sl=plan.sl,
            distance=f"{plan.risk:.{digits}f}", tp1=plan.tp1, tp2=plan.tp2, tp3=plan.tp3,
            risk=f"{s.get('risk_percent', 0):g}", maxloss=f"{s.get('max_money', 0):,.2f}",
            estloss=f"{s.get('total_money', 0):,.2f}", currency=s.get("currency", ""),
        )

    def _rejected(self, verdict: Verdict) -> None:
        args = {k: v for k, v in verdict.args.items()}
        text = self.t(f"auto.reject.{verdict.reason}", **args)
        # Everything is logged; only a good signal that a guard stopped is messaged.
        self._event("auto_reject", f"{verdict.reason}: {text}", epic=verdict.epic)
        if verdict.reason in ("order_failed", "data_error", "no_plan"):
            self._error(verdict.epic, text, always=verdict.reason == "order_failed")
            return
        if verdict.reason not in NOTIFY_REASONS:
            return
        key = f"{NOTIFIED_KEY}{verdict.epic}:{verdict.reason}"
        last = self.store.get(key)
        try:
            if last and self._now() - datetime.fromisoformat(last) < NOTICE_GAP:
                return
        except ValueError:
            pass
        self.store.set(key, self._now().isoformat())
        header = "auto.limit_reached" if verdict.reason == "daily_limit" else "auto.rejected"
        if verdict.reason == "daily_limit":
            today = self._now().astimezone(self._tz()).date().isoformat()
            if self.store.get(LIMIT_ALERT_KEY) == today:
                return
            self.store.set(LIMIT_ALERT_KEY, today)
        self.sup.notifier.send(self.t(header, epic=verdict.epic, reason=text), level="warn")

    def _error(self, epic: str, message: str, *, always: bool = False) -> None:
        self._event("auto_error", message, epic=epic)
        key = f"{ERROR_KEY}{epic}"
        last = self.store.get(key)
        try:
            recent = bool(last) and self._now() - datetime.fromisoformat(last) < NOTICE_GAP
        except ValueError:
            recent = False
        if recent and not always:
            return
        self.store.set(key, self._now().isoformat())
        self.sup.notifier.send(self.t("auto.error", epic=epic, error=message), level="error")

    # ------------------------------------------------------------------ status & report

    def status_command(self, argument: str = "") -> str:
        on = self.is_on()
        loss = self.daily_loss()
        try:
            open_count = len(self.sup.broker.positions())
        except Exception:
            open_count = None
        recent = [e for e in self.store.recent_events(60) if str(e["kind"]).startswith("auto_")][:4]
        lines = [self.t("auto.status",
                        state=self.t("auto.state_on") if on else self.t("auto.state_off"),
                        env=self.environment().upper(),
                        dry=self.t("auto.dry_run") if self.sup.config.dry_run else "",
                        risk=f"{self.risk_percent():g}",
                        instruments=", ".join(self.instruments()) or "-",
                        start=self.cfg.session_start, end=self.cfg.session_end,
                        session=self.t("auto.session_open") if self.in_session() else self.t("auto.session_closed"),
                        open=("-" if open_count is None else open_count),
                        cap=self.cfg.max_open_positions,
                        limit=f"{self.cfg.daily_loss_limit_percent:g}",
                        used=("-" if loss is None else f"{loss[1] / self.cfg.daily_loss_limit_percent * 100:.0f}"),
                        minimum=f"{self.cfg.min_signal_strength:g}")]
        if not self.cfg.enabled:
            lines.append(self.t("auto.not_enabled_short"))
        if loss and loss[1] >= self.cfg.daily_loss_limit_percent - 1e-9:
            lines.append(self.t("auto.status_blocked"))
        if recent:
            lines.append(self.t("auto.recent"))
            lines += [f"• {e['ts'][11:16]} {e['message'][:110]}" for e in recent]
        return "\n".join(lines)

    def daily_report_command(self, argument: str = "") -> str:
        state = self.day_state()
        if state is None:
            return self.t("auto.report_no_equity")
        today, start, equity = state
        change = equity - start
        tz = self._tz()
        day_plan = plan30_module.Plan30(start_date=self._now().astimezone(tz).date(),
                                        risk=self.risk_percent() / 100.0,
                                        loss_limit=self.cfg.daily_loss_limit_percent / 100.0)
        fills = self.sup.plan30._fills(day_plan)
        found = plan30_module.evaluate_day(fills, day_plan, 1, start, tz, self.sup.plan30._value_of)
        try:
            positions = self.sup.broker.positions()
            floating = sum(p.upl for p in positions if getattr(p, "upl", None) is not None)
            held = len(positions)
        except Exception:
            floating, held = None, None
        midnight = datetime.combine(self._now().astimezone(tz).date(), time_of_day(0), tzinfo=tz)
        events = self.store.events_since(midnight.astimezone(self._now().tzinfo))
        count = lambda kind: sum(1 for e in events if e["kind"] == kind)  # noqa: E731
        limit_money = start * self.cfg.daily_loss_limit_percent / 100.0
        used = max(0.0, -change) / limit_money * 100.0 if limit_money else 0.0
        if found is None:
            results = self.t("auto.report_results_unknown")
        elif not found.trades:
            results = self.t("auto.report_results_none")
        else:
            results = self.t("auto.report_results", trades=found.trades, wins=found.wins,
                             losses=found.losses, net=f"{found.net:+,.2f}",
                             costs=f"{found.costs:,.2f}",
                             dd="-" if found.max_dd is None else f"{found.max_dd:.2f}%",
                             estimated=self.t("p30.estimated") if found.estimated else "")
        return self.t(
            "auto.report", date=today, start=f"{start:,.2f}", equity=f"{equity:,.2f}",
            change=f"{change:+,.2f}", pct=f"{change / start * 100:+.2f}",
            results=results,
            floating="-" if floating is None else f"{floating:+,.2f}",
            held="-" if held is None else held, cap=self.cfg.max_open_positions,
            used=f"{used:.0f}", limit=f"{self.cfg.daily_loss_limit_percent:g}",
            risk=f"{self.risk_percent():g}", maxrisk=f"{equity * self.risk_percent() / 100:,.2f}",
            state=self.t("auto.state_on") if self.is_on() else self.t("auto.state_off"),
            orders=count("auto_order"), opened=count("auto_open"),
            rejected=count("auto_reject"), errors=count("auto_error"),
        )
