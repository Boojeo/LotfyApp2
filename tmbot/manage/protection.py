"""Broker-side protection: is the stop really on the server, and does it survive an outage?

Two kinds of protection exist and they are kept apart in every message:

* BROKER-SIDE: the stop-loss and take-profit stored on the broker's server.
  They are active whether or not this program, this computer or this internet
  connection is running.  Every change the bot makes to them goes to the broker
  as a modification of the position.
* DYNAMIC: what only works while this program runs -- the move to break-even,
  the halfway risk cut, the trailing stop, the reversal failsafe, the TP3
  extension.  If the app is offline these simply stop; the last stop the app set
  stays on the broker and keeps protecting the position.

What this module adds is a check that the first kind is *really there*:

* after every change and every ``protection.audit_seconds``, each managed trade
  is compared with the broker (after a grace period following our own change,
  because a broker can take a moment to show it);
* after a reconnect and at start-up, every trade is re-read from the broker and
  verified BEFORE normal monitoring resumes;
* a stop that is missing or looser than the one the bot last set is put back
  (never looser than the planned risk, so a repair can only reduce risk);
  a stop that is tighter than ours is adopted, not fought; a target that is
  missing is put back, a target that differs is only reported (it may be your own
  edit and is never overridden);
* a repair uses its own idempotency key, waits ``repair_cooldown_seconds``
  between attempts and gives up after ``max_repairs``, so it can neither send a
  duplicate order nor fight the broker in a loop;
* while ``/pause`` is on, or in dry-run, it only alerts -- ``/pause`` means no
  order modifications, and that is kept.

A failed repair never marks the trade ERROR: the audit is a safety net and must
not be the thing that stops a trade being managed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..errors import NotSupportedError, PermanentError, RetryableError
from ..models import BrokerPosition, Direction, ManagedTrade, TradeStatus, utcnow

log = logging.getLogger(__name__)

PAUSED_KEY = "paused"
OK = "ok"
STOP_MISSING, STOP_LOOSE, STOP_TIGHT = "stop_missing", "stop_loose", "stop_tight"
TARGET_MISSING, TARGET_DIFFERENT = "target_missing", "target_different"
ALERT_GAP = 600.0           # seconds between repeats of the same alert


@dataclass
class Finding:
    kind: str
    expected: Optional[float] = None
    actual: Optional[float] = None


class ProtectionService:
    def __init__(self, supervisor: Any):
        self.sup = supervisor
        self._next = 0.0
        self._touched: Dict[str, float] = {}
        self._last_repair: Dict[str, float] = {}
        self._alerted: Dict[str, float] = {}

    # ------------------------------------------------------------------ plumbing

    @property
    def t(self):
        return self.sup.t

    @property
    def cfg(self):
        return self.sup.config.protection

    def touched(self, trade: ManagedTrade) -> None:
        """Note that we just changed this deal at the broker: give it time to show."""
        self._touched[trade.deal_id] = time.monotonic()

    def _tolerance(self, trade: ManagedTrade) -> float:
        try:
            places = self.sup.broker.market_rules(trade.epic).decimal_places
        except Exception:
            places = 2
        return 10.0 ** (-places) * 1.01

    # ------------------------------------------------------------------ comparing

    def classify(self, trade: ManagedTrade, position: BrokerPosition) -> List[Finding]:
        """Everything that differs between what the bot expects and the broker holds."""
        tol = self._tolerance(trade)
        sign = trade.direction.sign
        found: List[Finding] = []

        expected_stop, actual_stop = trade.sl, position.stop_level
        if actual_stop is None:
            found.append(Finding(STOP_MISSING, expected_stop, None))
        elif (actual_stop - expected_stop) * sign < -tol:      # on the losing side of ours
            found.append(Finding(STOP_LOOSE, expected_stop, actual_stop))
        elif (actual_stop - expected_stop) * sign > tol:       # tighter than ours
            found.append(Finding(STOP_TIGHT, expected_stop, actual_stop))

        expected_target = self.sup.broker_target(trade)
        actual_target = position.profit_level
        if actual_target is None:
            found.append(Finding(TARGET_MISSING, expected_target, None))
        elif abs(actual_target - expected_target) > tol:
            found.append(Finding(TARGET_DIFFERENT, expected_target, actual_target))
        return found

    # ------------------------------------------------------------------ the periodic audit

    def step(self, positions: Dict[str, BrokerPosition]) -> None:
        """Called every tick; checks at most every ``audit_seconds``."""
        if not self.cfg.enabled:
            return
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + self.cfg.audit_seconds
        if getattr(self.sup, "_degraded", False):
            return
        try:
            lines: List[str] = []
            for trade in self.sup.store.active_trades():
                position = positions.get(trade.deal_id)
                if position is None or trade.status is not TradeStatus.MANAGING:
                    continue
                if now - self._touched.get(trade.deal_id, -1e9) < self.cfg.grace_seconds:
                    continue
                lines += self._handle(trade, position)
            if lines:
                self.sup.notifier.send(self.t("prot.header_audit") + "\n" + "\n".join(lines),
                                       level="warn")
        except Exception:
            log.exception("protection audit failed")      # a safety net must not raise

    # ------------------------------------------------------------------ after a reconnect

    def full_audit(self, reason: str) -> str:
        """Re-read every managed trade from the broker and verify its protection.

        Runs at start-up and right after a reconnect, before monitoring resumes,
        ignoring the throttle and the grace period.  Always sends a summary, so a
        quiet reconnect is visibly a checked one.
        """
        if not self.cfg.enabled:
            return ""
        trades = [t for t in self.sup.store.active_trades()
                  if t.status is TradeStatus.MANAGING]
        if not trades:
            return ""
        try:
            positions = {p.deal_id: p for p in self.sup.broker.positions()}
        except Exception as exc:
            text = self.t("prot.audit_unreadable", reason=self.t(f"prot.reason.{reason}"),
                          error=exc)
            self.sup.notifier.send(text, level="error")
            return text
        lines: List[str] = []
        for trade in trades:
            position = positions.get(trade.deal_id)
            if position is None:
                try:
                    position = self.sup.engine.resync(trade)   # a remapped deal id
                except Exception:
                    position = None
            if position is None:
                lines.append(self.t("prot.gone", epic=trade.epic, id=trade.short_id))
                continue
            trade.remaining_size = position.size
            trade.stop_level = position.stop_level
            self.sup.store.save_trade(trade)
            issues = self._handle(trade, position, force=True)
            lines += issues or [self.t(
                "prot.verified", epic=trade.epic, id=trade.short_id,
                stop=self._fmt(position.stop_level), target=self._fmt(position.profit_level))]
        text = self.t("prot.header_full", reason=self.t(f"prot.reason.{reason}"),
                      count=len(trades)) + "\n" + "\n".join(lines)
        self.sup.notifier.send(text)
        self.sup.store.log_event("protection_audit", f"{reason}: {len(trades)} trade(s) checked")
        return text

    # ------------------------------------------------------------------ handling a mismatch

    def _handle(self, trade: ManagedTrade, position: BrokerPosition, *, force: bool = False) -> List[str]:
        lines: List[str] = []
        for finding in self.classify(trade, position):
            if finding.kind == STOP_TIGHT:
                # The broker holds a better stop than ours (you tightened it by hand):
                # take it as ours instead of fighting it.
                trade.sl = finding.actual
                trade.stop_level = finding.actual
                self.sup.store.save_trade(trade)
                self.sup.store.log_event(
                    "protection_adopt", f"broker stop {finding.actual} is tighter than "
                    f"{finding.expected}: adopted", deal_id=trade.deal_id, epic=trade.epic)
                lines.append(self.t("prot.adopted", epic=trade.epic, id=trade.short_id,
                                    level=self._fmt(finding.actual)))
            elif finding.kind == TARGET_DIFFERENT:
                if self._should_alert(trade, finding.kind):
                    lines.append(self.t("prot.target_different", epic=trade.epic,
                                        id=trade.short_id, expected=self._fmt(finding.expected),
                                        actual=self._fmt(finding.actual)))
            else:
                lines.append(self._repair(trade, position, finding, force=force))
        return [line for line in lines if line]

    def _should_alert(self, trade: ManagedTrade, kind: str) -> bool:
        key = f"{trade.deal_id}:{kind}"
        now = time.monotonic()
        if now - self._alerted.get(key, -1e9) < ALERT_GAP:
            return False
        self._alerted[key] = now
        return True

    def _repair(self, trade: ManagedTrade, position: BrokerPosition, finding: Finding,
                *, force: bool) -> str:
        store = self.sup.store
        is_stop = finding.kind in (STOP_MISSING, STOP_LOOSE)
        what = "stop" if is_stop else "target"
        params = dict(epic=trade.epic, id=trade.short_id, what=self.t(f"prot.what.{what}"),
                      expected=self._fmt(finding.expected), actual=self._fmt(finding.actual))
        paused = store.get(PAUSED_KEY) == "1"
        if paused or self.sup.config.dry_run:
            if not self._should_alert(trade, finding.kind):
                return ""
            return self.t("prot.alert_paused" if paused else "prot.alert_dry", **params)

        failures = int(store.get(f"prot_fail:{trade.deal_id}:{what}") or 0)
        now = time.monotonic()
        if failures >= self.cfg.max_repairs:
            if not self._should_alert(trade, finding.kind + ":gaveup"):
                return ""
            return self.t("prot.gave_up", n=failures, **params)
        if not force and now - self._last_repair.get(trade.deal_id, -1e9) < self.cfg.repair_cooldown_seconds:
            return ""

        self._last_repair[trade.deal_id] = now
        sequence = int(store.get(f"prot_seq:{trade.deal_id}") or 0) + 1
        store.set(f"prot_seq:{trade.deal_id}", str(sequence))
        key = f"{trade.deal_id}:repair_{what}:{sequence}"
        if not store.begin_action(key, trade.deal_id, "SET_STOP" if is_stop else "SET_TARGET",
                                  f"protection repair: {finding.kind}"):
            return ""        # this exact attempt is already recorded: never send it twice
        try:
            if is_stop:
                self.sup.broker.modify_position(trade.deal_id, stop_level=finding.expected)
            else:
                self.sup.broker.modify_position(trade.deal_id, profit_level=finding.expected)
        except RetryableError as exc:
            store.release_action(key)
            log.warning("protection repair deferred on %s: %s", trade.short_id, exc)
            return self.t("prot.repair_deferred", error=exc, **params)
        except (PermanentError, NotSupportedError) as exc:
            store.fail_action(key, str(exc))
            store.set(f"prot_fail:{trade.deal_id}:{what}", str(failures + 1))
            store.log_event("protection_failed", f"{what} repair refused: {exc}",
                            deal_id=trade.deal_id, epic=trade.epic)
            return self.t("prot.repair_refused", error=exc, **params)
        store.complete_action(key, "", f"{what} repaired")
        self.touched(trade)

        # confirm on the server, not just that the request was accepted
        confirmed = self._confirm(trade, finding, is_stop)
        store.log_event("protection_repair", f"{finding.kind}: {what} -> {finding.expected} "
                        f"({'confirmed' if confirmed else 'NOT confirmed'})",
                        deal_id=trade.deal_id, epic=trade.epic)
        if confirmed:
            store.set(f"prot_fail:{trade.deal_id}:{what}", "0")
            return self.t("prot.repaired", **params)
        store.set(f"prot_fail:{trade.deal_id}:{what}", str(failures + 1))
        return self.t("prot.repair_unconfirmed", **params)

    def _confirm(self, trade: ManagedTrade, finding: Finding, is_stop: bool) -> bool:
        try:
            position = self.sup.broker.position(trade.deal_id)
        except Exception:
            return False
        if position is None:
            return False
        tol = self._tolerance(trade)
        actual = position.stop_level if is_stop else position.profit_level
        return actual is not None and abs(actual - finding.expected) <= tol

    # ------------------------------------------------------------------ /protect

    def _fmt(self, value: Optional[float]) -> str:
        return "-" if value is None else f"{value:g}"

    def command(self, argument: str) -> str:
        """/protect [id|verify]: what is protected where, and what needs the app."""
        argument = argument.strip()
        if argument.lower() == "verify":
            return self.full_audit("manual") or self.t("prot.nothing")
        trades = [t for t in self.sup.store.active_trades() if t.status is TradeStatus.MANAGING]
        if argument:
            chosen = self.sup.store.trade_by_short_id(argument)
            if chosen is None:
                return self.t("adoption.not_found", id=argument)
            trades = [chosen]
        if not trades:
            return self.t("prot.nothing")
        try:
            positions = {p.deal_id: p for p in self.sup.broker.positions()}
        except Exception as exc:
            return self.t("prot.audit_unreadable", reason=self.t("prot.reason.manual"), error=exc)
        return "\n\n".join(self.describe(trade, positions.get(trade.deal_id)) for trade in trades)

    def describe(self, trade: ManagedTrade, position: Optional[BrokerPosition]) -> str:
        """Broker-side protection first, then what depends on the app running."""
        config = self.sup.config.management
        sign = trade.direction.sign
        risk = trade.initial_risk or 0.0
        broker_stop = position.stop_level if position else None
        broker_target = position.profit_level if position else None
        findings = {f.kind for f in self.classify(trade, position)} if position else set()

        def state(kinds: tuple) -> str:
            if position is None:
                return self.t("prot.state_unknown")
            return self.t("prot.state_bad") if findings & set(kinds) else self.t("prot.state_ok")

        lines = [self.t("prot.title", epic=trade.epic, id=trade.short_id,
                        side=self.t.term("deal", trade.direction.value),
                        size=trade.remaining_size),
                 self.t("prot.broker_heading")]
        stop = broker_stop if broker_stop is not None else trade.sl
        r_at_stop = (stop - trade.entry_price) * sign / risk if risk else 0.0
        money, over = self._stop_money(trade, stop)
        lines.append(self.t(
            "prot.broker_stop", level=self._fmt(broker_stop), r=f"{r_at_stop:+.2f}",
            money=f" = {money}" if money != "-" else "", state=state((STOP_MISSING, STOP_LOOSE)),
            what=self.t("prot.locks" if r_at_stop > 0 else "prot.risks")))
        stage = (trade.leg_target.value if trade.leg_target else "TP3")
        lines.append(self.t("prot.broker_target", level=self._fmt(broker_target), stage=stage,
                            state=state((TARGET_MISSING, TARGET_DIFFERENT))))
        lines.append(self.t("prot.offline_note"))

        lines.append(self.t("prot.dynamic_heading"))
        if not trade.breakeven_done:
            lines.append(self.t("prot.dyn_breakeven", stage=config.breakeven_stage,
                                level=self._fmt(self._stage_price(trade, config.breakeven_stage))))
        if config.risk_cut_at > 0 and not trade.breakeven_done:
            trigger = trade.entry_price + sign * config.risk_cut_at * abs(trade.tp1 - trade.entry_price)
            lines.append(self.t("prot.dyn_risk_cut", level=self._fmt(round(trigger, 5)),
                                left=f"{config.risk_cut_to:.0%}"))
        lines.append(self.t("prot.dyn_trail", stage=config.trail_after_stage))
        if self.sup.config.reversal.enabled:
            lines.append(self.t("prot.dyn_reversal", action=self.sup.config.reversal.action))
        if config.extend_tp3 and trade.is_runner:
            lines.append(self.t("prot.dyn_extend"))
        lines.append(self.t("prot.dyn_offline"))

        atr_line = self._atr_line(trade)
        if atr_line:
            lines.append(atr_line)
        if money == "-":
            lines.append(self.t("prot.money_unknown"))
        else:
            lines.append(self.t("prot.limit_over" if over else "prot.limit_ok", money=money))
        return "\n".join(lines)

    def _stage_price(self, trade: ManagedTrade, stage: str) -> float:
        return {"TP1": trade.tp1, "TP2": trade.tp2, "TP3": trade.tp3}.get(stage.upper(),
                                                                         trade.entry_price)

    def _stop_money(self, trade: ManagedTrade, stop: float) -> tuple:
        """(money text, over the risk limit?) for what this deal loses at ``stop``."""
        sign = trade.direction.sign
        try:
            rules = self.sup.broker.market_rules(trade.epic)
            equity = self._equity()
        except Exception:
            return "-", False
        loss_distance = max(0.0, (trade.entry_price - stop) * sign)
        gain_distance = max(0.0, (stop - trade.entry_price) * sign)
        value = rules.value_per_point
        if not value:
            return "-", False
        size = trade.remaining_size
        if loss_distance > 0:
            money = loss_distance * value * size
            limit = equity * self.sup.config.report.risk_percent / 100.0 if equity else None
            legs = trade.basket_size or 1
            return f"-{money:,.2f}", bool(limit and money * legs > limit + 1e-9)
        return f"+{gain_distance * value * size:,.2f}", False

    def _equity(self) -> Optional[float]:
        from ..analysis import style as style_module
        return style_module.equity_of(self.sup.broker.account_summary())

    def _atr_line(self, trade: ManagedTrade) -> str:
        """Reference only: where the ATR-based stops would sit now. Nothing is applied."""
        from .rules import chandelier_stop, trail_multiplier
        try:
            snapshot = self.sup._snapshot(trade.epic, trade.management_timeframe)
        except Exception:
            return ""
        if snapshot.atr <= 0:
            return ""
        price = snapshot.quote.exit_price(trade.direction)
        sign = trade.direction.sign
        stop = trade.stop_level if trade.stop_level is not None else trade.sl
        gap = (price - stop) * sign / snapshot.atr
        multiplier = trail_multiplier(snapshot.strength, self.sup.config.management)
        trail = (chandelier_stop(trade, snapshot, multiplier, self.sup.config.management)
                 if multiplier else None)
        lock = price - sign * self.sup.config.reversal.tighten_atr * snapshot.atr
        return self.t("prot.atr", gap=f"{gap:.1f}", atr=f"{snapshot.atr:.4g}",
                      trail="-" if trail is None else f"{trail:.5g}",
                      lock=f"{lock:.5g}", tighten=f"{self.sup.config.reversal.tighten_atr:g}")
