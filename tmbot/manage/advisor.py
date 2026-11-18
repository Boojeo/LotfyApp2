"""Exit advice for open trades, and the commands that act on it -- only when asked.

Each cycle, every managed deal gets a recommendation (``analysis.exit_advice``)
built on historical test odds.  The bot messages you when it changes; it never
acts on it.  Acting is yours, per deal, and goes through the engine's normal
paths (the same ones /close and the ladder use):

  /exit [id]          show the advice for one deal or all of them
  /do <id>            apply the advice for that deal, once (close, partial close
                      or move the stop) -- refused if it has gone stale or changed
  /skip <id>          cancel the advice for that deal until it changes
  /targets <id> 2|3   T1-T2 only: pull the basket's final target in to TP2 and
                      set that take-profit on the broker; 3 puts TP3 back

Nothing executes by itself, so the engine's broker-side targets, TP3 extension,
trailing stop and reversal failsafe all keep working exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..analysis import exit_advice as advice_module
from ..analysis import odds as odds_module
from ..analysis.exit_advice import CLOSE, HOLD, MOVE_STOP, PARTIAL_CLOSE, Advice
from ..models import ManagedTrade, MarketSnapshot, Stage, TradeStatus, utcnow
from .rules import Decision, DecisionKind, Evaluation

log = logging.getLogger(__name__)

ADVICE_KEY = "advice:"         # + deal id: the latest advice, as JSON
STATE_KEY = "advice_state:"    # + deal id: what was last messaged, and what was skipped
ODDS_TTL = 300.0               # seconds a tested-odds result is reused


class ExitAdvisor:
    def __init__(self, supervisor: Any):
        self.sup = supervisor
        self._odds: Dict[tuple, Tuple[float, odds_module.OddsResult]] = {}

    # ------------------------------------------------------------------ plumbing

    @property
    def t(self):
        return self.sup.t

    @property
    def cfg(self):
        return self.sup.config.exit_advice

    @property
    def store(self):
        return self.sup.store

    def _now(self) -> datetime:
        return utcnow()

    # ------------------------------------------------------------------ computing

    def _odds_for(self, trade: ManagedTrade, snapshot: MarketSnapshot) -> Optional[odds_module.OddsResult]:
        """Historical reach rates for the targets still ahead, from the current price."""
        geometry = advice_module.geometry(trade, snapshot, self.sup.config.management)
        if geometry is None or not geometry[1]:
            return None
        stop_atr, targets = geometry
        timeframe = trade.management_timeframe or self.sup.config.management.management_timeframe
        key = (trade.epic, timeframe, trade.direction.value, round(stop_atr, 1),
               tuple(sorted((s, round(d, 1)) for s, d in targets.items())))
        cached = self._odds.get(key)
        if cached and time.monotonic() - cached[0] < ODDS_TTL:
            return cached[1]
        try:
            history = self.sup._cached_candles(trade.epic, timeframe, self.cfg.history_bars)
        except Exception as exc:
            log.warning("%s: no history for the odds (%s)", trade.epic, exc)
            return None
        result = odds_module.reach_rates(
            history, trade.direction, stop_atr, targets,
            horizon=self.cfg.horizon_bars, min_samples=self.cfg.min_samples)
        self._odds[key] = (time.monotonic(), result)
        return result

    def compute(self, trade: ManagedTrade, snapshot: MarketSnapshot, signal=None) -> Advice:
        return advice_module.advise(
            trade, snapshot, self._odds_for(trade, snapshot), self.sup.config.management,
            self.sup.config.reversal, self.cfg, signal=signal)

    def _advice_id(self, advice: Advice) -> str:
        raw = f"{advice.deal_id}:{advice.action}:{advice.stop_level}:{advice.size}:{self._now():%Y%m%d%H%M%S}"
        return hashlib.sha1(raw.encode()).hexdigest()[:8]

    def _save(self, trade: ManagedTrade, advice: Advice) -> str:
        advice_id = self._advice_id(advice)
        self.store.set(f"{ADVICE_KEY}{trade.deal_id}", json.dumps({
            "id": advice_id, "at": self._now().isoformat(), "advice": advice.to_dict()}))
        return advice_id

    def _load(self, trade: ManagedTrade) -> Optional[dict]:
        raw = self.store.get(f"{ADVICE_KEY}{trade.deal_id}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    # ------------------------------------------------------------------ every cycle

    def update(self, trade: ManagedTrade, snapshot: MarketSnapshot, evaluation: Evaluation) -> None:
        """Called by the supervisor for each managed deal; never raises."""
        if not self.cfg.enabled or trade.status is not TradeStatus.MANAGING:
            return
        try:
            advice = self.compute(trade, snapshot, evaluation.reversal)
            advice_id = self._save(trade, advice)
            self._maybe_message(trade, advice, advice_id)
        except Exception:
            log.exception("exit advice failed for %s", trade.short_id)

    def _state(self, trade: ManagedTrade) -> dict:
        raw = self.store.get(f"{STATE_KEY}{trade.deal_id}")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _maybe_message(self, trade: ManagedTrade, advice: Advice, advice_id: str) -> None:
        state = self._state(trade)
        signature = f"{advice.action}:{advice.best_stage}:{advice.stop_level}"
        previous = state.get("signature", f"{HOLD}:None:None")
        if signature == previous or signature == state.get("skipped"):
            return
        previous_action = previous.split(":")[0]
        # A message for a recommendation to act, and one when it goes back to HOLD.
        if advice.action == HOLD and previous_action == HOLD:
            self._remember(trade, state, signature)          # only the target view changed
            return
        last = state.get("sent_at")
        if last and self._now() - datetime.fromisoformat(last) < timedelta(
                minutes=self.cfg.advice_cooldown_minutes):
            return
        state.update(signature=signature, sent_at=self._now().isoformat())
        state.pop("skipped", None)
        self.store.set(f"{STATE_KEY}{trade.deal_id}", json.dumps(state))
        self.store.log_event("exit_advice", f"{advice.action} {trade.short_id}: "
                             f"{[r[0] for r in advice.reasons][:3]}",
                             deal_id=trade.deal_id, epic=trade.epic)
        self.sup.notifier.send(self.render(trade, advice, advice_id), level="warn")

    def _remember(self, trade: ManagedTrade, state: dict, signature: str) -> None:
        state["signature"] = signature
        self.store.set(f"{STATE_KEY}{trade.deal_id}", json.dumps(state))

    # ------------------------------------------------------------------ rendering

    def render(self, trade: ManagedTrade, advice: Advice, advice_id: str = "") -> str:
        t = self.t
        stage = trade.leg_target.value if trade.leg_target else "-"
        lines = [t("adv.title", epic=trade.epic, id=trade.short_id,
                   side=t.term("deal", trade.direction.value), stage=stage,
                   r=f"{advice.r_now:+.2f}"),
                 t("adv.action", action=t(f"adv.action.{advice.action}"))]
        detail = t(f"adv.detail.{advice.action}", level=advice.stop_level,
                   size=advice.size) if advice.action in (MOVE_STOP, PARTIAL_CLOSE) else ""
        if detail:
            lines.append(detail)
        lines.append(t("adv.why"))
        lines += [f"  • {t(f'adv.reason.{key}', **self._args(args))}" for key, args in advice.reasons]
        for view in advice.targets:
            if view.p is not None:
                lines.append(t("adv.target", stage=view.stage, level=view.level,
                               dist=f"{view.distance_atr:.1f}", p=f"{view.p * 100:.0f}",
                               low=f"{view.low * 100:.0f}", high=f"{view.high * 100:.0f}",
                               q=f"{view.q * 100:.0f}", ev=f"{view.ev_r:+.2f}",
                               now=f"{advice.r_now:+.2f}", n=advice.samples))
            else:
                lines.append(t("adv.target_none", stage=view.stage, level=view.level,
                               dist=f"{view.distance_atr:.1f}"))
        if advice.best_stage:
            lines.append(t("adv.best", stage=advice.best_stage))
        lines.append(t("adv.honest"))
        lines.append(self._options(trade, advice))
        return "\n".join(lines)

    def _args(self, args: dict) -> dict:
        shown = dict(args)
        if "momentum" in shown:
            shown["momentum"] = self.t(f"adv.momentum.{shown['momentum']}")
        if "strength" in shown:
            shown["strength"] = self.t.term("strength", shown["strength"])
        return shown

    def _options(self, trade: ManagedTrade, advice: Advice) -> str:
        t, sid = self.t, trade.short_id
        parts = []
        if advice.action != HOLD:
            parts += [t("adv.opt_do", id=sid, action=t(f"adv.action.{advice.action}")),
                      t("adv.opt_skip", id=sid)]
        if trade.is_runner or self.sup.config.management.exit_model != "three_deals":
            parts += [t("adv.opt_targets", id=sid)]
        return "\n".join(parts) if parts else ""

    # ------------------------------------------------------------------ /exit

    def exit_command(self, argument: str) -> str:
        argument = argument.strip()
        trades = [x for x in self.store.active_trades() if x.status is TradeStatus.MANAGING]
        if argument:
            chosen = self.store.trade_by_short_id(argument)
            if chosen is None:
                return self.t("adoption.not_found", id=argument)
            trades = [chosen]
        if not trades:
            return self.t("adv.nothing")
        out: List[str] = []
        for trade in trades:
            try:
                snapshot = self.sup._snapshot(trade.epic, trade.management_timeframe)
                advice = self.compute(trade, snapshot)
            except Exception as exc:
                out.append(self.t("adv.unavailable", epic=trade.epic, id=trade.short_id, error=exc))
                continue
            advice_id = self._save(trade, advice)
            out.append(self.render(trade, advice, advice_id))
        return "\n\n".join(out)

    # ------------------------------------------------------------------ /do and /skip

    def do_command(self, argument: str) -> str:
        trade = self.store.trade_by_short_id(argument.strip())
        if trade is None:
            return self.t("adoption.not_found", id=argument.strip())
        saved = self._load(trade)
        if not saved:
            return self.t("adv.no_advice", id=trade.short_id)
        age = self._now() - datetime.fromisoformat(saved["at"])
        if age > timedelta(minutes=self.cfg.stale_minutes):
            return self.t("adv.stale", id=trade.short_id, minutes=f"{self.cfg.stale_minutes:g}")
        shown = saved["advice"]
        if shown["action"] == HOLD:
            return self.t("adv.nothing_to_do", id=trade.short_id)
        # Re-check against the market as it is NOW: confirm what you were shown.
        try:
            snapshot = self.sup._snapshot(trade.epic, trade.management_timeframe)
            fresh = self.compute(trade, snapshot)
        except Exception as exc:
            return self.t("adv.unavailable", epic=trade.epic, id=trade.short_id, error=exc)
        if fresh.action != shown["action"] or fresh.stop_level != shown.get("stop_level"):
            self._save(trade, fresh)
            return self.t("adv.changed", id=trade.short_id,
                          old=self.t(f"adv.action.{shown['action']}"),
                          new=self.t(f"adv.action.{fresh.action}"))
        decision = self._decision(trade, fresh, snapshot, saved["id"])
        applied = self.sup.engine.apply(trade, Evaluation(decisions=[decision], blocked=[]))
        self.store.save_trade(trade)
        self.sup.protection.touched(trade)
        self.store.set(f"{ADVICE_KEY}{trade.deal_id}", "")        # consumed: one confirmation, one action
        self.store.log_event("advice_applied", f"{fresh.action} confirmed by user",
                             deal_id=trade.deal_id, epic=trade.epic)
        return "\n".join(applied) if applied else self.t("adv.not_accepted")

    def _decision(self, trade: ManagedTrade, advice: Advice, snapshot: MarketSnapshot,
                  advice_id: str) -> Decision:
        price = snapshot.quote.exit_price(trade.direction)
        base = dict(deal_id=trade.deal_id, reason=f"exit advice confirmed ({advice.action})")
        if advice.action == MOVE_STOP:
            return Decision(kind=DecisionKind.SET_STOP, key=f"{trade.deal_id}:advice_stop:{advice_id}",
                            reason_key="reason.advice_stop", reason_args={"level": advice.stop_level},
                            stop_level=advice.stop_level, **base)
        if advice.action == PARTIAL_CLOSE:
            return Decision(kind=DecisionKind.PARTIAL_CLOSE,
                            key=f"{trade.deal_id}:manual_advice_partial:{advice_id}",
                            reason_key="reason.advice_close", reason_args={},
                            size=advice.size, exit_price=price, **base)
        return Decision(kind=DecisionKind.CLOSE_ALL,
                        key=f"{trade.deal_id}:manual_advice_close:{advice_id}",
                        reason_key="reason.advice_close", reason_args={},
                        size=trade.remaining_size, exit_price=price, **base)

    def skip_command(self, argument: str) -> str:
        trade = self.store.trade_by_short_id(argument.strip())
        if trade is None:
            return self.t("adoption.not_found", id=argument.strip())
        saved = self._load(trade)
        if not saved:
            return self.t("adv.no_advice", id=trade.short_id)
        shown = saved["advice"]
        state = self._state(trade)
        state["skipped"] = f"{shown['action']}:{shown.get('best_stage')}:{shown.get('stop_level')}"
        self.store.set(f"{STATE_KEY}{trade.deal_id}", json.dumps(state))
        self.store.set(f"{ADVICE_KEY}{trade.deal_id}", "")
        self.store.log_event("advice_skipped", f"{shown['action']} cancelled by user",
                             deal_id=trade.deal_id, epic=trade.epic)
        return self.t("adv.skipped", id=trade.short_id, action=self.t(f"adv.action.{shown['action']}"))

    # ------------------------------------------------------------------ /targets

    def targets_command(self, argument: str) -> str:
        """/targets <id> 2  -> T1-T2 only;  /targets <id> 3  -> back to T1-T2-T3."""
        parts = argument.split()
        if len(parts) != 2 or parts[1] not in ("2", "3"):
            return self.t("adv.targets_usage")
        trade = self.store.trade_by_short_id(parts[0])
        if trade is None:
            return self.t("adoption.not_found", id=parts[0])
        legs = self.store.trades_in_group(trade.group_id) if trade.group_id else [trade]
        runners = [leg for leg in (legs or [trade]) if leg.is_runner
                   and leg.status is TradeStatus.MANAGING]
        if not runners:
            return self.t("adv.no_runner", id=trade.short_id)
        out: List[str] = []
        for runner in runners:
            out.append(self._set_final_target(runner, parts[1] == "2"))
        return "\n".join(out)

    def _set_final_target(self, runner: ManagedTrade, pull_in: bool) -> str:
        t = self.t
        management = self.sup.config.management
        if pull_in:
            new_level = runner.tp2
            if not runner.tp3_plan:
                runner.tp3_plan = runner.tp3
            elif abs(runner.tp3 - runner.tp2) < 1e-9:
                return t("adv.targets_already", id=runner.short_id, mode="T1–T2")
        else:
            if not runner.tp3_plan:
                return t("adv.targets_already", id=runner.short_id, mode="T1–T2–T3")
            new_level = runner.tp3_plan
        try:
            snapshot = self.sup._snapshot(runner.epic, runner.management_timeframe)
            price = snapshot.quote.exit_price(runner.direction)
            margin = snapshot.rules.stop_buffer(price) if snapshot.rules else 0.0
        except Exception as exc:
            return t("adv.unavailable", epic=runner.epic, id=runner.short_id, error=exc)
        if (new_level - price) * runner.direction.sign <= margin:
            return t("adv.targets_passed", id=runner.short_id, level=new_level, price=price)
        previous = (runner.tp3, runner.tp3_extensions)
        runner.tp3 = new_level
        runner.tp3_extensions = management.tp3_max_extensions if pull_in else 0
        decision = Decision(
            kind=DecisionKind.SET_TARGET, deal_id=runner.deal_id,
            key=f"{runner.deal_id}:targets_{'2' if pull_in else '3'}:{self._now():%Y%m%d%H%M%S}",
            reason=f"final target set to {new_level} by user",
            reason_key="reason.advice_target", reason_args={"level": new_level},
            profit_level=new_level, stage=Stage.TP3)
        applied = self.sup.engine.apply(runner, Evaluation(decisions=[decision], blocked=[]))
        if not applied:
            runner.tp3, runner.tp3_extensions = previous       # the broker did not take it
            if runner.status is TradeStatus.ERROR:
                # A refused target change leaves the stop and the position as they
                # were: it must not stop the trade being managed.
                runner.status = TradeStatus.MANAGING
            self.store.save_trade(runner)
            return t("adv.not_accepted")
        if not pull_in:
            runner.tp3_plan = 0.0
        self.store.save_trade(runner)
        self.sup.protection.touched(runner)
        self.store.log_event("targets", f"final target {'TP2' if pull_in else 'TP3'} {new_level}",
                             deal_id=runner.deal_id, epic=runner.epic)
        return t("adv.targets_set_2" if pull_in else "adv.targets_set_3",
                 id=runner.short_id, level=new_level)
