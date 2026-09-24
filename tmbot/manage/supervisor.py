"""The daemon: adopt positions, manage them, publish reports.

One thread owns every broker mutation.  Telegram commands run on their own
thread but only ever read state or flip a flag the loop reads next cycle, so
there is no lock around the decision path.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as time_of_day, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..analysis.bias import trend_strength
from ..analysis.indicators import adx, atr, ema, last_two, last_value, macd, swing_points
from ..analysis import chart as chart_module
from ..analysis import journal as journal_module
from ..analysis.report import ReportBuilder, render_markdown, render_text
from ..broker.base import BrokerAdapter, PartialCloseStrategy
from ..config import Config, resolve_timezone
from ..errors import AuthError, RetryableError, StaleDataError
from ..i18n import LANGUAGES, Translator
from ..models import (
    Fill,
    Bias,
    BrokerPosition,
    Candle,
    ManagedTrade,
    MarketSnapshot,
    Quote,
    Stage,
    TradePlan,
    TradeStatus,
    utcnow,
)
from ..notify.base import Notifier
from ..store import Store
from ..util.timeframes import seconds as timeframe_seconds
from .engine import TradeEngine
from .rules import Decision, DecisionKind, Evaluation, evaluate

log = logging.getLogger(__name__)

PAUSED_KEY = "paused"
LANGUAGE_KEY = "language"
LAST_REPORT_KEY = "last_daily_report"
LAST_REFRESH_KEY = "last_intraday_refresh"


@dataclass
class _CachedCandles:
    fetched_at: float
    candles: List[Candle]


@dataclass
class Supervisor:
    broker: BrokerAdapter
    store: Store
    config: Config
    notifier: Notifier
    reporter: Optional[ReportBuilder] = None
    engine: Optional[TradeEngine] = None

    t: Translator = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _candles: Dict[str, _CachedCandles] = field(default_factory=dict, init=False)
    _quotes: Dict[str, Quote] = field(default_factory=dict, init=False)
    _degraded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        # A language chosen with /lang outlives a restart; config is the default.
        stored = self.store.get(LANGUAGE_KEY)
        self.t = Translator(stored if stored in LANGUAGES else self.config.language)
        if self.reporter is None:
            self.reporter = ReportBuilder(self.broker, self.config)
        if self.engine is None:
            self.engine = TradeEngine(
                self.broker, self.store, self.config, self.notifier, self.t
            )

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.broker.connect()
        partials_needed = self.config.management.exit_model == "partial_close"
        probe = self.broker.probe_partial_close(needed=partials_needed)
        log.info("\n%s", probe.render())
        if probe.blocking and self.config.management.ladder:
            self.notifier.send(
                self.t("startup.partials_unavailable", detail=probe.render()),
                level="warn",
            )
        if self.config.management.exit_model == "three_deals" and probe.hedging_mode is False:
            self.notifier.send(self.t("startup.hedging_off"), level="error")
        if self.broker.algo_trading_enabled() is False:
            self.notifier.send(self.t("startup.algo_trading_off"), level="error")
        self._reconcile_pending_actions()
        self._register_commands()
        self.notifier.set_tag(f"[{self.config.broker.environment.upper()}] ")
        self.notifier.set_translator(self.t)
        self.notifier.start()
        account = self.broker.account_summary()
        epics = self.t.join(item.epic for item in self.config.analysis.watchlist) or "-"
        self.notifier.send(
            self.t("startup.online",
                   environment=self.config.broker.environment,
                   dry_run=self.t("startup.dry_run") if self.config.dry_run else "",
                   account=account.get("accountId", "?"))
            + "\n" + self.t("startup.watching", epics=epics)
            + "\n" + self.t("startup.exit_model", model=self.config.management.exit_model)
            + (f"  ({probe.strategy.value})" if partials_needed else "")
        )

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Blocking main loop."""
        self.start()
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self.tick()
                except (RetryableError, AuthError) as exc:
                    self._enter_degraded(str(exc))
                except Exception:
                    log.exception("unhandled error in supervisor tick")
                elapsed = time.monotonic() - started
                self._stop.wait(max(1.0, self.config.management.poll_seconds - elapsed))
        finally:
            self.notifier.stop()
            self.broker.close()

    # ------------------------------------------------------------------ one cycle

    def tick(self) -> None:
        positions = self.broker.positions()
        if self._degraded:
            self._leave_degraded()

        by_deal_id = {position.deal_id: position for position in positions}
        self._detect_new_positions(positions)
        self._expire_pending_confirmations()
        self._manage_open_trades(by_deal_id)
        self._run_schedules()

    # ------------------------------------------------------------------ adoption

    def _detect_new_positions(self, positions: List[BrokerPosition]) -> None:
        known = self.store.known_deal_ids()
        for position in positions:
            if position.deal_id in known:
                continue
            try:
                plan = self._plan_for(position.epic, direction=position.direction)
            except Exception as exc:
                log.exception("could not build a plan for %s", position.epic)
                self.notifier.send(
                    self.t("adoption.no_plan",
                           direction=self.t.direction_name(position.direction),
                           size=position.size, epic=position.epic,
                           price=position.entry_price, error=exc),
                    level="error",
                )
                continue

            group_id, leg_index, leg_target = self._assign_leg(position)
            trade = ManagedTrade.from_position(
                position, plan,
                group_id=group_id, leg_index=leg_index, leg_target=leg_target,
            )
            # Rebasing onto the fill reintroduces float noise; round to the
            # instrument's own precision before any of it reaches the broker.
            try:
                rules = self.broker.market_rules(trade.epic)
                trade.tp1 = rules.round_price(trade.tp1)
                trade.tp2 = rules.round_price(trade.tp2)
                trade.tp3 = rules.round_price(trade.tp3)
                trade.sl = rules.round_price(trade.sl)
            except (RetryableError, AuthError) as exc:
                log.warning("%s: could not round levels to market precision (%s)",
                            trade.epic, exc)

            # A leg arriving after its basket was already confirmed is adopted
            # on the same decision rather than asking again.
            siblings = self.store.trades_in_group(group_id)
            if any(leg.status is TradeStatus.MANAGING for leg in siblings):
                trade.status = TradeStatus.MANAGING
                self.store.save_trade(trade)
                applied = self._apply_initial_protection(trade)
                self.notifier.send(
                    self.t("adoption.late_leg", epic=trade.epic, index=leg_index + 1,
                           size=trade.initial_size, price=trade.entry_price,
                           target=leg_target.value if leg_target else "TP3")
                    + "\n" + "\n".join(applied)
                )
                continue

            trade.status = TradeStatus.PENDING_CONFIRMATION
            self.store.save_trade(trade)
            self.store.log_event(
                "adoption_pending",
                f"{position.direction.value} {position.size} @ {position.entry_price}",
                deal_id=trade.deal_id, epic=trade.epic,
            )
            self.notifier.send(self._adoption_message(trade, plan))

    def _assign_leg(self, position: BrokerPosition) -> tuple[str, int, Optional[Stage]]:
        """Work out which three-deal basket a new position belongs to.

        Deals on the same instrument and side, opened within the grouping
        window, are treated as one basket: first deal exits at TP1, second at
        TP2, third rides to TP3.
        """
        management = self.config.management
        if management.exit_model != "three_deals":
            return "", 0, None

        targets = [stage.upper() for stage in management.leg_targets]
        window = management.group_window_minutes * 60.0
        # Deals are grouped by how close together THEY were opened, not by how
        # old they are -- otherwise starting the bot an hour after you placed
        # them would scatter the basket into three separate groups.
        reference = position.created_at or utcnow()

        open_groups: Dict[str, List[ManagedTrade]] = {}
        for trade in self.store.trades_with_status(
            TradeStatus.MANAGING, TradeStatus.PENDING_CONFIRMATION
        ):
            if not trade.group_id or trade.epic != position.epic:
                continue
            if trade.direction is not position.direction:
                continue
            opened = trade.opened_at or trade.adopted_at
            if abs((opened - reference).total_seconds()) > window:
                continue
            open_groups.setdefault(trade.group_id, []).append(trade)

        for group_id, legs in sorted(
            open_groups.items(),
            key=lambda item: max(leg.adopted_at for leg in item[1]),
            reverse=True,
        ):
            if len(legs) >= len(targets):
                continue
            leg_index = max(leg.leg_index for leg in legs) + 1
            return group_id, leg_index, Stage(targets[min(leg_index, len(targets) - 1)])

        return f"{position.epic}-{position.direction.value}-{uuid.uuid4().hex[:6]}", 0, Stage(
            targets[0]
        )

    def _adoption_message(self, trade: ManagedTrade, plan: TradePlan) -> str:
        management = self.config.management
        parts = [
            self.t("adoption.detected", epic=trade.epic,
                   direction=self.t.direction_name(trade.direction),
                   size=trade.initial_size, price=trade.entry_price),
            self.t("adoption.plan", plan_id=plan.plan_id,
                   bias=self.t.bias_name(plan.bias))
            + ("" if plan.direction is trade.direction else self.t("adoption.against_bias")),
            self.t("adoption.levels", sl=trade.sl, tp1=trade.tp1,
                   tp2=trade.tp2, tp3=trade.tp3),
        ]

        rules = self.t("adoption.plan_rules",
                       breakeven=management.breakeven_stage,
                       trail=management.trail_after_stage)

        if management.exit_model == "three_deals":
            target = trade.leg_target.value if trade.leg_target else "TP3"
            legs = len(management.leg_targets)
            parts.append(
                self.t("adoption.plan_leg", index=trade.leg_index + 1,
                       total=legs, target=target) + self.t.semicolon + rules
            )
            outstanding = legs - len(self.store.trades_in_group(trade.group_id))
            if outstanding > 0:
                parts.append(self.t("adoption.waiting_legs", count=outstanding,
                                    minutes=f"{management.group_window_minutes:.0f}",
                                    target=target))
        else:
            steps = self.t.join(
                f"{step.stage} {step.fraction:.0%}" for step in management.ladder
            ) or "-"
            parts.append(
                self.t("adoption.plan_ladder", steps=steps) + self.t.semicolon + rules
            )

        parts.append("")
        parts.append(self.t("adoption.confirm_hint", id=trade.short_id))
        return "\n".join(parts)

    def _plan_for(
        self,
        epic: str,
        *,
        force: bool = False,
        direction: Optional[Direction] = None,
    ) -> TradePlan:
        """Fetch today's plan, rebuilding it when it does not fit the trade.

        A stored plan whose direction opposes the position being adopted is
        useless for managing it -- its stop sits on the wrong side of the entry
        -- so the levels are recomputed for the side actually being traded.
        """
        if not force:
            existing = self.store.latest_plan(epic, max_age_hours=24)
            if existing and (direction is None or existing.direction is direction):
                return existing
            if existing:
                log.info(
                    "%s: stored plan is %s but the position is %s; rebuilding levels",
                    epic, existing.direction.value, direction.value,
                )
        plan = self.reporter.build(epic, direction=direction)
        self.store.save_plan(plan)
        return plan

    def confirm(self, short_id: str) -> str:
        trade = self.store.trade_by_short_id(short_id)
        if trade is None:
            return self.t("adoption.not_found", id=short_id)
        if trade.status is not TradeStatus.PENDING_CONFIRMATION:
            return self.t("adoption.already", epic=trade.epic, id=trade.short_id,
                          status=trade.status.value)
        # One reply adopts every leg of the basket -- asking three times for
        # three deals opened as one trade is friction, not safety.
        legs = [
            leg for leg in (self.store.trades_in_group(trade.group_id) or [trade])
            if leg.status is TradeStatus.PENDING_CONFIRMATION
        ] or [trade]

        lines: List[str] = []
        for leg in legs:
            leg.status = TradeStatus.MANAGING
            leg.adopted_at = utcnow()
            self.store.save_trade(leg)
            self.store.log_event(
                "adopted", "confirmed by user", deal_id=leg.deal_id, epic=leg.epic
            )
            applied = self._apply_initial_protection(leg)
            label = (
                self.t("adoption.label_leg", index=leg.leg_index + 1,
                       target=leg.leg_target.value)
                if leg.leg_target else self.t("adoption.label_position")
            )
            lines.append(
                self.t("adoption.managing", epic=leg.epic,
                       direction=self.t.direction_name(leg.direction),
                       size=leg.remaining_size, price=leg.entry_price, label=label)
            )
            lines.extend(f"  {line}" for line in applied)
        return "\n".join(lines)

    def decline(self, short_id: str) -> str:
        trade = self.store.trade_by_short_id(short_id)
        if trade is None:
            return self.t("adoption.not_found", id=short_id)
        trade.status = TradeStatus.DECLINED
        self.store.save_trade(trade)
        self.store.log_event("declined", "declined by user", deal_id=trade.deal_id, epic=trade.epic)
        return self.t("adoption.declined", epic=trade.epic, id=trade.short_id)

    def _apply_initial_protection(self, trade: ManagedTrade) -> List[str]:
        """Put the plan's stop and final target on the position we just adopted."""
        decisions: List[Decision] = []
        if trade.stop_level is None or abs(trade.stop_level - trade.sl) > 1e-9:
            decisions.append(Decision(
                kind=DecisionKind.SET_STOP,
                deal_id=trade.deal_id,
                key=f"{trade.deal_id}:initial_stop",
                reason=f"initial protective stop from plan {trade.plan_id}",
                reason_key="reason.initial_stop",
                reason_args={"plan_id": trade.plan_id},
                stop_level=trade.sl,
            ))
        decisions.append(Decision(
            kind=DecisionKind.SET_TARGET,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:initial_target",
            reason=f"final target from plan {trade.plan_id}",
            reason_key="reason.initial_target",
            reason_args={"plan_id": trade.plan_id},
            profit_level=trade.tp3,
        ))
        return self.engine.apply(trade, Evaluation(decisions=decisions, blocked=[]))

    def _expire_pending_confirmations(self) -> None:
        management = self.config.management
        if not management.auto_decline_on_timeout:
            return
        deadline = timedelta(minutes=management.adoption_confirm_timeout_minutes)
        for trade in self.store.pending_trades():
            if utcnow() - trade.adopted_at < deadline:
                continue
            trade.status = TradeStatus.DECLINED
            trade.note = "auto-declined: confirmation timed out"
            self.store.save_trade(trade)
            self.notifier.send(
                self.t("adoption.timeout", epic=trade.epic, id=trade.short_id,
                       minutes=f"{management.adoption_confirm_timeout_minutes:.0f}"),
                level="warn",
            )

    def manage_existing(self, short_id: str) -> str:
        """Adopt a position that was previously declined or timed out."""
        trade = self.store.trade_by_short_id(short_id)
        if trade is None:
            for position in self.broker.positions():
                if position.deal_id.endswith(short_id):
                    plan = self._plan_for(position.epic)
                    trade = ManagedTrade.from_position(position, plan)
                    break
        if trade is None:
            return self.t("adoption.not_found", id=short_id)
        trade.status = TradeStatus.MANAGING
        self.store.save_trade(trade)
        applied = self._apply_initial_protection(trade)
        return self.t("adoption.managing", epic=trade.epic,
                      direction=self.t.direction_name(trade.direction),
                      size=trade.remaining_size, price=trade.entry_price,
                      label=self.t("adoption.label_position")) + "\n" + "\n".join(applied)

    # ------------------------------------------------------------------ management

    def _manage_open_trades(self, positions: Dict[str, BrokerPosition]) -> None:
        paused = self.store.get(PAUSED_KEY) == "1"
        for trade in self.store.active_trades():
            position = positions.get(trade.deal_id)
            if position is None:
                position = self.engine.resync(trade)
                if position is None:
                    self._finalise(trade)
                    continue
            else:
                trade.remaining_size = position.size
                trade.stop_level = position.stop_level

            try:
                snapshot = self._snapshot(trade.epic)
            except (RetryableError, StaleDataError) as exc:
                log.warning("%s: no usable market data (%s); skipping this cycle", trade.epic, exc)
                continue

            if snapshot.rules is not None and not snapshot.rules.tradeable:
                # Closed market: the quote is a stale last price and any modify
                # would be rejected, which would wrongly flag the trade as ERROR.
                log.debug("%s is closed; holding %s as it is", trade.epic, trade.short_id)
                self.store.save_trade(trade)
                continue

            price = snapshot.quote.exit_price(trade.direction)
            self._absorb_extremes(trade, snapshot)
            trade.update_best_price(price)

            evaluation = evaluate(
                trade, snapshot, self.config.management, self.config.reversal
            )
            if evaluation.reversal is not None:
                trade.reversal_streak = evaluation.reversal.streak
            if paused:
                if evaluation.decisions:
                    log.info(
                        "paused: withholding %d action(s) on %s",
                        len(evaluation.decisions), trade.epic,
                    )
                self.store.save_trade(trade)
                continue

            applied = self.engine.apply(trade, evaluation)
            self._maybe_alert_reversal(trade, evaluation, snapshot, price, applied)
            self.store.save_trade(trade)
            if applied:
                self.notifier.send(
                    self.t("action.header", epic=trade.epic, id=trade.short_id,
                           price=price, r=f"{trade.r_multiple(price):+.2f}")
                    + "\n" + "\n".join(f"- {line}" for line in applied)
                )
            if trade.status is TradeStatus.CLOSED:
                self._finalise(trade, notify=False)

    def _maybe_alert_reversal(
        self,
        trade: ManagedTrade,
        evaluation: Evaluation,
        snapshot: MarketSnapshot,
        price: float,
        applied: List[str],
    ) -> None:
        """Tell the user the trend turned, once per episode.

        The alert fires whether or not the bot could act, because the decision
        that remains -- hold, or get out -- is the user's. It is rate-limited
        so a market chopping either side of a cross cannot spam the phone.
        """
        signal = evaluation.reversal
        if signal is None or not signal.confirmed or trade.reversal_muted:
            return
        cooldown = timedelta(minutes=self.config.reversal.cooldown_minutes)
        if trade.reversal_handled_at and utcnow() - trade.reversal_handled_at < cooldown:
            return
        trade.reversal_handled_at = utcnow()

        lines = [
            self.t("reversal.header", epic=trade.epic, id=trade.short_id),
            self.t("reversal.evidence", agreeing=signal.agreeing,
                   total=len(signal.signals), adx=f"{snapshot.adx:.0f}",
                   detail=signal.summary(self.t)),
            self.t("reversal.position",
                   direction=self.t.direction_name(trade.direction),
                   r=f"{trade.r_multiple(price):+.2f}"),
        ]
        if applied:
            lines.append(self.t("reversal.acted", action="; ".join(applied)))
        else:
            lines.append(self.t("reversal.no_action"))
        lines.append(self.t("reversal.options", id=trade.short_id))

        self.store.log_event("reversal", signal.summary(),
                             deal_id=trade.deal_id, epic=trade.epic)
        self.notifier.send("\n".join(lines), level="warn")

    def journal_command(self, argument: str) -> str:
        """How the system has actually performed, in R."""
        try:
            days = int(argument.strip()) if argument.strip() else 30
        except ValueError:
            days = 30
        return journal_module.render(
            journal_module.build(self.store, max(1, days)), self.t
        )

    def hold_command(self, argument: str) -> str:
        """Mute reversal alerts for one trade, or turn them back on."""
        trade = self.store.trade_by_short_id(argument.strip())
        if trade is None:
            return self.t("adoption.not_found", id=argument.strip())
        trade.reversal_muted = not trade.reversal_muted
        self.store.save_trade(trade)
        key = "reversal.muted" if trade.reversal_muted else "reversal.unmuted"
        return self.t(key, epic=trade.epic, id=trade.short_id)

    def _absorb_extremes(self, trade: ManagedTrade, snapshot: MarketSnapshot) -> None:
        """Fold recent candle extremes into the high-water mark.

        Polling can miss a spike between cycles; the trailing stop should still
        credit the trade for the move it actually made.
        """
        opened = trade.opened_at or trade.adopted_at
        for candle in snapshot.candles[-10:]:
            if candle.ts < opened:
                continue
            trade.update_best_price(candle.high if trade.direction.sign > 0 else candle.low)

    def _finalise(self, trade: ManagedTrade, *, notify: bool = True) -> None:
        self._record_broker_exit(trade)
        trade.status = TradeStatus.CLOSED
        trade.closed_at = trade.closed_at or utcnow()
        trade.remaining_size = 0.0
        self.store.save_trade(trade)
        self.store.log_event("closed", trade.note or "position no longer open",
                             deal_id=trade.deal_id, epic=trade.epic)
        if notify:
            stage = (
                "TP2" if trade.tp2_done
                else "TP1" if trade.tp1_done
                else self.t("action.stage_none")
            )
            self.notifier.send(
                self.t("action.closed_at_broker", epic=trade.epic,
                       id=trade.short_id, stage=stage)
            )

    def _record_broker_exit(self, trade: ManagedTrade) -> None:
        """Record a position the broker closed on us -- a stop-out or a target.

        We never saw the fill, so the exit price is estimated from the last
        quote and the fill is flagged ``inferred``. The journal counts it and
        says how many of its numbers rest on an estimate rather than pretending
        the figure is exact.
        """
        if trade.remaining_size <= 0 or self.store.has_fill(trade.deal_id, "BROKER"):
            return

        # The quote cache is empty after a restart, which is exactly when this
        # runs: the machine died, the position hit its stop while the bot was
        # off, and the first cycle back finds it gone. Returning here would
        # drop the trade from the journal entirely and quietly flatter the
        # results, so fall back rather than give up.
        # Some brokers (MT5) keep the real fill in their deal history -- use it
        # when there, and only estimate when there is nothing better.
        try:
            actual = self.broker.closing_price(trade.deal_id)
        except Exception as exc:
            log.warning("%s: could not read the closing deal (%s)", trade.epic, exc)
            actual = None
        price = actual
        quote = self._quotes.get(trade.epic)
        if price is not None:
            pass
        elif quote is not None:
            price = quote.exit_price(trade.direction)
        else:
            try:
                price = self.broker.quote(trade.epic).exit_price(trade.direction)
            except Exception as exc:
                log.warning("%s: no quote to price the exit (%s)", trade.epic, exc)
        if price is None:
            # Unattended closes are usually stop-outs; the stop is the better
            # estimate than nothing at all.
            price = trade.stop_level if trade.stop_level is not None else trade.entry_price
            log.warning("%s: pricing the exit from the stop level %s",
                        trade.epic, price)

        fill = Fill(
            deal_id=trade.deal_id, epic=trade.epic, ts=utcnow(), stage="BROKER",
            size=trade.remaining_size, price=price,
            entry_price=trade.entry_price, direction=trade.direction,
            initial_risk=trade.initial_risk,
            fraction=min(1.0, trade.remaining_size / trade.initial_size)
            if trade.initial_size else 0.0,
            inferred=actual is None,
        )
        self.store.record_fill(fill)
        trade.realised = round(trade.realised + fill.r_multiple, 6)

    # ------------------------------------------------------------------ snapshots

    def _snapshot(self, epic: str) -> MarketSnapshot:
        management = self.config.management
        timeframe = management.management_timeframe
        try:
            quote = self.broker.quote(epic)
            self._quotes[epic] = quote
        except RetryableError:
            quote = self._quotes.get(epic)
            if quote is None:
                raise
            if quote.age_seconds() > management.max_quote_age_seconds:
                raise StaleDataError(
                    f"{epic}: last quote is {quote.age_seconds():.0f}s old"
                )
            log.warning("%s: using cached quote (%.0fs old)", epic, quote.age_seconds())

        candles = self._cached_candles(epic, timeframe, management.management_lookback)
        atr_value = last_value(atr(candles, management.trail_atr_period)) or 0.0
        adx_series, plus_di, minus_di = adx(candles, 14)
        adx_value = last_value(adx_series) or 0.0
        closes = [candle.close for candle in candles]
        highs, lows = swing_points(candles, management.swing_left, management.swing_right)

        # The previous bar's readings as well as the current ones: a cross is
        # only visible as a change between two bars.
        fast_now, fast_prev = last_two(ema(closes, 20))
        slow_now, slow_prev = last_two(ema(closes, 50))
        plus_now, plus_prev = last_two(plus_di)
        minus_now, minus_prev = last_two(minus_di)
        _, _, histogram = macd(closes)
        hist_now, hist_prev = last_two(histogram)

        return MarketSnapshot(
            epic=epic,
            quote=quote,
            candles=candles,
            atr=atr_value,
            adx=adx_value,
            ema_fast=fast_now or quote.mid,
            ema_slow=slow_now or quote.mid,
            strength=trend_strength(adx_value, management),
            swing_high=highs[-1][1] if highs else None,
            swing_low=lows[-1][1] if lows else None,
            rules=self.broker.market_rules(epic),
            plus_di=plus_now,
            minus_di=minus_now,
            plus_di_prev=plus_prev,
            minus_di_prev=minus_prev,
            ema_fast_prev=fast_prev,
            ema_slow_prev=slow_prev,
            macd_hist=hist_now,
            macd_hist_prev=hist_prev,
        )

    def _cached_candles(self, epic: str, timeframe: str, limit: int) -> List[Candle]:
        key = f"{epic}:{timeframe}"
        # Refetch at a quarter of the bar length: often enough to see a new bar
        # promptly, rarely enough to stay well inside the rate limits.
        ttl = max(self.config.management.poll_seconds, timeframe_seconds(timeframe) / 4)
        cached = self._candles.get(key)
        if cached and time.monotonic() - cached.fetched_at < ttl:
            return cached.candles
        candles = self.broker.candles(epic, timeframe, limit)
        self._candles[key] = _CachedCandles(time.monotonic(), candles)
        return candles

    # ------------------------------------------------------------------ degraded mode

    def _enter_degraded(self, reason: str) -> None:
        if not self._degraded:
            self._degraded = True
            self.store.log_event("degraded", reason)
            self.notifier.send(
                self.t("degraded.lost", broker=self.broker.name, reason=reason),
                level="error",
            )
        else:
            log.warning("still degraded: %s", reason)

    def _leave_degraded(self) -> None:
        self._degraded = False
        self.store.log_event("recovered", "broker reachable again")
        self.notifier.send(self.t("degraded.recovered"))

    def _reconcile_pending_actions(self) -> None:
        """Resolve anything that was in flight when the process last stopped."""
        pending = self.store.pending_actions()
        if not pending:
            return
        log.info("reconciling %d in-flight action(s) from the previous run", len(pending))
        for action in pending:
            trade = self.store.trade(action["deal_id"])
            # The broker is the source of truth: drop the claim and let the next
            # evaluation decide again from the real position state.
            self.store.release_action(action["key"])
            if trade:
                self.engine.resync(trade)
                self.store.save_trade(trade)
            self.store.log_event(
                "reconciled", f"released in-flight {action['kind']} ({action['key']})",
                deal_id=action["deal_id"],
            )

    # ------------------------------------------------------------------ schedules

    def _run_schedules(self) -> None:
        self._maybe_daily_report()
        self._maybe_intraday_refresh()

    def _report_timezone(self):
        # Validated at startup, so this cannot silently become UTC.
        return resolve_timezone(self.config.report.timezone)

    def _maybe_daily_report(self) -> None:
        watchlist = self.config.analysis.watchlist
        if not watchlist:
            return
        tz = self._report_timezone()
        now_local = utcnow().astimezone(tz)
        try:
            hour, minute = (int(part) for part in self.config.report.daily_time.split(":"))
        except ValueError:
            log.error("invalid report.daily_time %r", self.config.report.daily_time)
            return
        scheduled = datetime.combine(now_local.date(), time_of_day(hour, minute), tzinfo=tz)
        if now_local < scheduled:
            return
        if self.store.get(LAST_REPORT_KEY) == now_local.date().isoformat():
            return
        self.store.set(LAST_REPORT_KEY, now_local.date().isoformat())
        self.publish_daily_report()

    def publish_daily_report(self) -> None:
        for item in self.config.analysis.watchlist:
            try:
                plan = self._plan_for(item.epic, force=True)
            except Exception as exc:
                log.exception("daily report failed for %s", item.epic)
                self.notifier.send(
                    self.t("report.failed", epic=item.epic, error=exc), level="error"
                )
                continue
            self._publish_plan(plan)

    def _publish_plan(self, plan: TradePlan, *, notify: bool = True) -> str:
        """Write the markdown, draw the chart, and push both out."""
        self._write_report_file(plan)
        caption = render_text(plan, self.t)
        if not notify:
            return caption
        path = self._render_chart(plan)
        if path:
            self.notifier.send_photo(str(path), caption)
        else:
            self.notifier.send(caption)
        return caption

    def _render_chart(self, plan: TradePlan) -> Optional[Path]:
        """Draw the plan.  A failed chart must never cost you the text plan."""
        report = self.config.report
        if not report.charts:
            return None
        try:
            candles = self._cached_candles(
                plan.epic, report.chart_timeframe, max(report.chart_bars * 2, 200)
            )
            directory = Path(self.config.resolved_report_dir)
            path = directory / f"{plan.epic}-{plan.created_at:%Y%m%d-%H%M}.png"
            return chart_module.render(
                plan, candles, path, t=self.t,
                theme=report.chart_theme, bars=report.chart_bars,
            )
        except Exception as exc:
            log.warning("%s: chart could not be drawn (%s)", plan.epic, exc)
            self.notifier.send(self.t("chart.unavailable", error=exc), level="warn")
            return None

    def _write_report_file(self, plan: TradePlan) -> None:
        directory = Path(self.config.resolved_report_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{plan.epic}-{plan.created_at:%Y%m%d-%H%M}.md"
            path.write_text(render_markdown(plan, self.t), encoding="utf-8")
            log.info("wrote %s", path)
        except OSError as exc:
            log.warning("could not write report file: %s", exc)

    def _maybe_intraday_refresh(self) -> None:
        interval = self.config.report.intraday_refresh_hours
        if interval <= 0 or not self.config.analysis.watchlist:
            return
        raw = self.store.get(LAST_REFRESH_KEY)
        if raw:
            try:
                last = datetime.fromisoformat(raw)
                if utcnow() - last < timedelta(hours=interval):
                    return
            except ValueError:
                pass
        self.store.set(LAST_REFRESH_KEY, utcnow().isoformat())
        self.refresh_plans()

    def refresh_plans(self) -> None:
        """Rebuild each watchlist plan and report only what changed."""
        for item in self.config.analysis.watchlist:
            previous = self.store.latest_plan(item.epic)
            try:
                plan = self._plan_for(item.epic, force=True)
            except Exception as exc:
                log.warning("intraday refresh failed for %s: %s", item.epic, exc)
                continue
            if previous is None:
                continue
            messages: List[str] = []
            if self.config.report.notify_on_bias_flip and plan.bias is not previous.bias:
                messages.append(self.t(
                    "report.update_bias",
                    old=self.t.bias_name(previous.bias), new=self.t.bias_name(plan.bias),
                    confidence=f"{plan.confidence:.0f}",
                ))
            if self.config.report.notify_on_level_invalidated:
                messages.extend(self._invalidated_levels(previous, plan))
            if messages:
                self.notifier.send(
                    self.t("report.update_heading", epic=item.epic) + "\n"
                    + "\n".join(f"- {line}" for line in messages) + "\n"
                    + self.t("report.update_levels", sl=plan.sl, tp1=plan.tp1,
                             tp2=plan.tp2, tp3=plan.tp3),
                    level="warn",
                )

    def _invalidated_levels(self, previous: TradePlan, plan: TradePlan) -> List[str]:
        notes: List[str] = []
        price = plan.reference_price
        if previous.direction.is_beyond(price, previous.tp3):
            notes.append(f"price {price} has run past the old TP3 {previous.tp3}")
        elif not previous.direction.is_beyond(price, previous.sl) and (
            (previous.direction.sign > 0 and price < previous.sl)
            or (previous.direction.sign < 0 and price > previous.sl)
        ):
            notes.append(f"price {price} is through the old stop {previous.sl}")
        if previous.direction is not plan.direction:
            notes.append(
                f"trade direction flipped {previous.direction.value} -> {plan.direction.value}"
            )
        return notes

    # ------------------------------------------------------------------ commands

    def _register_commands(self) -> None:
        self.notifier.register("help", lambda _: self.help_text())
        self.notifier.register("status", lambda _: self.status_text())
        self.notifier.register("confirm", self.confirm)
        self.notifier.register("decline", self.decline)
        self.notifier.register("manage", self.manage_existing)
        self.notifier.register("report", self.report_command)
        self.notifier.register("plan", self.plan_command)
        self.notifier.register("close", self.close_command)
        self.notifier.register("be", self.breakeven_command)
        self.notifier.register("journal", self.journal_command)
        self.notifier.register("hold", self.hold_command)
        self.notifier.register("lang", self.set_language)
        self.notifier.register("pause", lambda _: self.set_paused(True))
        self.notifier.register("resume", lambda _: self.set_paused(False))

    def help_text(self) -> str:
        return self.t("command.help")

    def set_paused(self, paused: bool) -> str:
        self.store.set(PAUSED_KEY, "1" if paused else "0")
        message = self.t("status.paused_now" if paused else "status.resumed_now")
        self.store.log_event("paused" if paused else "resumed", message)
        return message

    def set_language(self, argument: str) -> str:
        """Switch display language.

        Only presentation changes -- stored trades, plans and enum values are
        untouched, so this is safe to run with positions open.
        """
        choice = argument.strip().lower()
        if choice not in LANGUAGES:
            return self.t("command.language_usage", current=self.t.language)
        self.t = self.t.with_language(choice)
        self.engine.t = self.t
        self.notifier.set_tag(f"[{self.config.broker.environment.upper()}] ")
        self.notifier.set_translator(self.t)
        self.store.set(LANGUAGE_KEY, choice)
        self.store.log_event("language", f"display language set to {choice}")
        return self.t("command.language_set")

    def status_text(self) -> str:
        lines: List[str] = []
        if self.store.get(PAUSED_KEY) == "1":
            lines.append(self.t("status.paused"))
        if self._degraded:
            lines.append(self.t("status.degraded"))
        for trade in self.store.pending_trades():
            lines.append(self.t(
                "status.awaiting", epic=trade.epic,
                direction=self.t.direction_name(trade.direction),
                size=trade.initial_size, price=trade.entry_price, id=trade.short_id,
            ))
        for trade in self.store.active_trades():
            try:
                price = self.broker.quote(trade.epic).exit_price(trade.direction)
                marker = f"{price} ({trade.r_multiple(price):+.2f}R)"
            except Exception:
                marker = self.t("status.price_unavailable")
            if trade.leg_target is not None:
                flags = f"leg{trade.leg_index + 1}->{trade.leg_target.value}"
                flags += "B" if trade.breakeven_done else ""
                flags += "T" if trade.trailing_active else ""
            else:
                flags = "".join([
                    "1" if trade.tp1_done else "-",
                    "2" if trade.tp2_done else "-",
                    "B" if trade.breakeven_done else "-",
                    "T" if trade.trailing_active else "-",
                ])
            lines.append(self.t(
                "status.line", epic=trade.epic,
                direction=self.t.direction_name(trade.direction),
                remaining=trade.remaining_size, initial=trade.initial_size,
                entry=trade.entry_price, marker=marker,
            ))
            lines.append(self.t(
                "status.levels", sl=trade.stop_level, tp1=trade.tp1,
                tp2=trade.tp2, tp3=trade.tp3, flags=flags, id=trade.short_id,
            ))
        return "\n".join(lines) if lines else self.t("status.empty")

    def report_command(self, argument: str) -> str:
        epics = [self.config.canonical_epic(argument)] if argument.strip() else [
            item.epic for item in self.config.analysis.watchlist
        ]
        if not epics:
            return self.t("command.no_epic")
        replies: List[str] = []
        for epic in epics:
            try:
                plan = self._plan_for(epic, force=True)
                replies.append(self._publish_plan(plan))
            except Exception as exc:
                replies.append(self.t("report.failed", epic=epic, error=exc))
        return "\n\n".join(replies)

    def plan_command(self, argument: str) -> str:
        epic = self.config.canonical_epic(argument)
        if not epic:
            return self.t("command.plan_usage")
        plan = self.store.latest_plan(epic)
        return render_text(plan, self.t) if plan else self.t("report.no_plan", epic=epic)

    def close_command(self, argument: str) -> str:
        trade = self.store.trade_by_short_id(argument.strip())
        if trade is None:
            return self.t("adoption.not_found", id=argument.strip())
        decision = Decision(
            kind=DecisionKind.CLOSE_ALL,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:manual_close:{utcnow():%Y%m%d%H%M%S}",
            reason="manual close requested",
            reason_key="reason.manual_close",
            size=trade.remaining_size,
        )
        applied = self.engine.apply(trade, Evaluation(decisions=[decision], blocked=[]))
        self.store.save_trade(trade)
        return "\n".join(applied) if applied else "Close was not accepted; check the logs."

    def breakeven_command(self, argument: str) -> str:
        trade = self.store.trade_by_short_id(argument.strip())
        if trade is None:
            return self.t("adoption.not_found", id=argument.strip())
        decision = Decision(
            kind=DecisionKind.SET_STOP,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:manual_be:{utcnow():%Y%m%d%H%M%S}",
            reason="manual break-even requested",
            reason_key="reason.manual_breakeven",
            stop_level=trade.entry_price,
        )
        applied = self.engine.apply(trade, Evaluation(decisions=[decision], blocked=[]))
        self.store.save_trade(trade)
        return "\n".join(applied) if applied else "Stop move was not accepted; check the logs."
