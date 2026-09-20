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
from typing import Dict, List, Optional

from ..analysis.bias import trend_strength
from ..analysis.indicators import adx, atr, ema, last_value, swing_points
from ..analysis.report import ReportBuilder, render_markdown, render_text
from ..broker.base import BrokerAdapter, PartialCloseStrategy
from ..config import Config
from ..errors import AuthError, RetryableError, StaleDataError
from ..models import (
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

    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _candles: Dict[str, _CachedCandles] = field(default_factory=dict, init=False)
    _quotes: Dict[str, Quote] = field(default_factory=dict, init=False)
    _degraded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.reporter is None:
            self.reporter = ReportBuilder(self.broker, self.config)
        if self.engine is None:
            self.engine = TradeEngine(self.broker, self.store, self.config, self.notifier)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.broker.connect()
        probe = self.broker.probe_partial_close()
        log.info("\n%s", probe.render())
        if probe.strategy is PartialCloseStrategy.UNSUPPORTED and self.config.management.ladder:
            self.notifier.send(
                "Partial closes are NOT available on this account:\n"
                + probe.render()
                + "\nTP1/TP2 partials will be skipped; break-even and trailing still apply.",
                level="warn",
            )
        if self.config.management.exit_model == "three_deals" and probe.hedging_mode is False:
            self.notifier.send(
                "Exit model is THREE_DEALS but this account has hedging OFF, so "
                "Capital.com will merge your three deals into one position and the "
                "legs cannot be closed separately.\nTurn hedging on in the "
                "Capital.com platform, or switch management.exit_model back to "
                "partial_close.",
                level="error",
            )
        self._reconcile_pending_actions()
        self._register_commands()
        self.notifier.start()
        account = self.broker.account_summary()
        self.notifier.send(
            f"Trade manager online ({self.config.broker.environment}"
            + (", DRY RUN" if self.config.dry_run else "")
            + f") on account {account.get('accountId', '?')}.\n"
            f"Watching: {', '.join(item.epic for item in self.config.analysis.watchlist) or '-'}\n"
            f"Partial close: {probe.strategy.value}"
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
                    f"New {position.direction.value} {position.size} {position.epic} "
                    f"@ {position.entry_price} detected, but no plan could be built "
                    f"({exc}). It is NOT being managed.",
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
                    f"{trade.epic}: leg {leg_index + 1} joined the confirmed group "
                    f"({trade.initial_size} @ {trade.entry_price}), exits at "
                    f"{leg_target.value if leg_target else 'TP3'}.\n"
                    + "\n".join(applied)
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
        agrees = plan.direction is trade.direction
        missing = ""
        if self.config.management.exit_model == "three_deals":
            target = trade.leg_target.value if trade.leg_target else "TP3"
            legs = len(self.config.management.leg_targets)
            ladder = (
                f"leg {trade.leg_index + 1} of {legs} -- this deal closes in full at {target}"
            )
            outstanding = legs - len(self.store.trades_in_group(trade.group_id))
            if outstanding > 0:
                missing = (
                    f"\nWAITING on {outstanding} more deal(s) to complete the basket. "
                    f"Open them within {self.config.management.group_window_minutes:.0f} "
                    f"minutes, or this deal closes at {target} on its own."
                )
        else:
            ladder = ", ".join(
                f"{step.stage} {step.fraction:.0%}" for step in self.config.management.ladder
            ) or "none"
        return (
            f"New position detected: {trade.epic} {trade.direction.value} "
            f"{trade.initial_size} @ {trade.entry_price}\n"
            f"Plan {plan.plan_id} -- bias {plan.bias.value}"
            + ("" if agrees else "  (NOTE: your entry is against the plan's bias)")
            + f"\nSL {trade.sl}   TP1 {trade.tp1}   TP2 {trade.tp2}   TP3 {trade.tp3}\n"
            f"Ladder: {ladder}; break-even at "
            f"{self.config.management.breakeven_stage}; "
            f"trail after {self.config.management.trail_after_stage}"
            + missing
            + f"\n\n/confirm {trade.short_id} to manage it, "
              f"/decline {trade.short_id} to leave it alone."
        )

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
            return f"No trade matching {short_id!r}."
        if trade.status is not TradeStatus.PENDING_CONFIRMATION:
            return f"{trade.epic} ({trade.short_id}) is already {trade.status.value}."
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
                f"leg {leg.leg_index + 1} -> {leg.leg_target.value}"
                if leg.leg_target else "position"
            )
            lines.append(
                f"Managing {leg.epic} {leg.direction.value} {leg.remaining_size} "
                f"@ {leg.entry_price} ({label})."
            )
            lines.extend(f"  {line}" for line in applied)
        return "\n".join(lines)

    def decline(self, short_id: str) -> str:
        trade = self.store.trade_by_short_id(short_id)
        if trade is None:
            return f"No trade matching {short_id!r}."
        trade.status = TradeStatus.DECLINED
        self.store.save_trade(trade)
        self.store.log_event("declined", "declined by user", deal_id=trade.deal_id, epic=trade.epic)
        return f"Leaving {trade.epic} ({trade.short_id}) unmanaged."

    def _apply_initial_protection(self, trade: ManagedTrade) -> List[str]:
        """Put the plan's stop and final target on the position we just adopted."""
        decisions: List[Decision] = []
        if trade.stop_level is None or abs(trade.stop_level - trade.sl) > 1e-9:
            decisions.append(Decision(
                kind=DecisionKind.SET_STOP,
                deal_id=trade.deal_id,
                key=f"{trade.deal_id}:initial_stop",
                reason=f"initial protective stop from plan {trade.plan_id}",
                stop_level=trade.sl,
            ))
        decisions.append(Decision(
            kind=DecisionKind.SET_TARGET,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:initial_target",
            reason=f"final target from plan {trade.plan_id}",
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
                f"{trade.epic} ({trade.short_id}) was not confirmed within "
                f"{management.adoption_confirm_timeout_minutes:.0f} minutes -- "
                "leaving it unmanaged. Use /manage to take it over later.",
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
            return f"No position matching {short_id!r}."
        trade.status = TradeStatus.MANAGING
        self.store.save_trade(trade)
        applied = self._apply_initial_protection(trade)
        return f"Managing {trade.epic} ({trade.short_id}).\n" + "\n".join(applied)

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

            price = snapshot.quote.exit_price(trade.direction)
            self._absorb_extremes(trade, snapshot)
            trade.update_best_price(price)

            evaluation = evaluate(trade, snapshot, self.config.management)
            if paused:
                if evaluation.decisions:
                    log.info(
                        "paused: withholding %d action(s) on %s",
                        len(evaluation.decisions), trade.epic,
                    )
                self.store.save_trade(trade)
                continue

            applied = self.engine.apply(trade, evaluation)
            self.store.save_trade(trade)
            if applied:
                self.notifier.send(
                    f"{trade.epic} ({trade.short_id}) @ {price} | "
                    f"{trade.r_multiple(price):+.2f}R\n" + "\n".join(f"- {line}" for line in applied)
                )
            if trade.status is TradeStatus.CLOSED:
                self._finalise(trade, notify=False)

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
        trade.status = TradeStatus.CLOSED
        trade.closed_at = trade.closed_at or utcnow()
        trade.remaining_size = 0.0
        self.store.save_trade(trade)
        self.store.log_event("closed", trade.note or "position no longer open",
                             deal_id=trade.deal_id, epic=trade.epic)
        if notify:
            self.notifier.send(
                f"{trade.epic} ({trade.short_id}) is closed at the broker. "
                f"Ladder reached: "
                f"{'TP2' if trade.tp2_done else 'TP1' if trade.tp1_done else 'none'}."
            )

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
        adx_series, _, _ = adx(candles, 14)
        adx_value = last_value(adx_series) or 0.0
        closes = [candle.close for candle in candles]
        highs, lows = swing_points(candles, management.swing_left, management.swing_right)

        return MarketSnapshot(
            epic=epic,
            quote=quote,
            candles=candles,
            atr=atr_value,
            adx=adx_value,
            ema_fast=last_value(ema(closes, 20)) or quote.mid,
            ema_slow=last_value(ema(closes, 50)) or quote.mid,
            strength=trend_strength(adx_value, management),
            swing_high=highs[-1][1] if highs else None,
            swing_low=lows[-1][1] if lows else None,
            rules=self.broker.market_rules(epic),
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
                f"Lost contact with {self.broker.name}: {reason}\n"
                "Holding all state; no decisions will be taken until the connection "
                "recovers. Positions already carry their broker-side stop.",
                level="error",
            )
        else:
            log.warning("still degraded: %s", reason)

    def _leave_degraded(self) -> None:
        self._degraded = False
        self.store.log_event("recovered", "broker reachable again")
        self.notifier.send("Connection to the broker recovered; management resumed.")

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

    def _report_timezone(self) -> timezone:
        name = self.config.report.timezone
        if name.upper() == "UTC":
            return timezone.utc
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)  # type: ignore[return-value]
        except Exception:
            log.warning("unknown timezone %r; falling back to UTC", name)
            return timezone.utc

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
                self.notifier.send(f"Report for {item.epic} failed: {exc}", level="error")
                continue
            self._write_report_file(plan)
            self.notifier.send(render_text(plan))

    def _write_report_file(self, plan: TradePlan) -> None:
        from pathlib import Path
        directory = Path(self.config.report.output_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{plan.epic}-{plan.created_at:%Y%m%d-%H%M}.md"
            path.write_text(render_markdown(plan), encoding="utf-8")
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
                messages.append(
                    f"bias {previous.bias.value} -> {plan.bias.value} "
                    f"(confidence {plan.confidence:.0f})"
                )
            if self.config.report.notify_on_level_invalidated:
                messages.extend(self._invalidated_levels(previous, plan))
            if messages:
                self.notifier.send(
                    f"{item.epic} plan update:\n"
                    + "\n".join(f"- {line}" for line in messages)
                    + f"\nNew levels: SL {plan.sl} | TP1 {plan.tp1} "
                      f"TP2 {plan.tp2} TP3 {plan.tp3}",
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
        self.notifier.register("pause", lambda _: self.set_paused(True))
        self.notifier.register("resume", lambda _: self.set_paused(False))

    def help_text(self) -> str:
        return (
            "/status            open positions and ladder state\n"
            "/confirm <id>      start managing a detected position\n"
            "/decline <id>      leave a detected position alone\n"
            "/manage <id>       take over a position declined earlier\n"
            "/report [epic]     rebuild and send the full plan\n"
            "/plan [epic]       show the stored plan levels\n"
            "/close <id>        close the remaining size now\n"
            "/be <id>           move the stop to entry now\n"
            "/pause /resume     stop or restart all order modifications"
        )

    def set_paused(self, paused: bool) -> str:
        self.store.set(PAUSED_KEY, "1" if paused else "0")
        state = "PAUSED -- no stops or targets will be modified" if paused else "resumed"
        self.store.log_event("paused" if paused else "resumed", state)
        return f"Trade management {state}."

    def status_text(self) -> str:
        lines: List[str] = []
        if self.store.get(PAUSED_KEY) == "1":
            lines.append("** management is PAUSED **")
        if self._degraded:
            lines.append("** broker connection degraded **")
        pending = self.store.pending_trades()
        for trade in pending:
            lines.append(
                f"[awaiting confirmation] {trade.epic} {trade.direction.value} "
                f"{trade.initial_size} @ {trade.entry_price} -> /confirm {trade.short_id}"
            )
        for trade in self.store.active_trades():
            try:
                price = self.broker.quote(trade.epic).exit_price(trade.direction)
                marker = f"{price} ({trade.r_multiple(price):+.2f}R)"
            except Exception:
                marker = "price unavailable"
            if trade.leg_target is not None:
                rungs = f"leg{trade.leg_index + 1}->{trade.leg_target.value}"
                rungs += "B" if trade.breakeven_done else ""
                rungs += "T" if trade.trailing_active else ""
            else:
                rungs = "".join([
                    "1" if trade.tp1_done else "-",
                    "2" if trade.tp2_done else "-",
                    "B" if trade.breakeven_done else "-",
                    "T" if trade.trailing_active else "-",
                ])
            lines.append(
                f"{trade.epic} {trade.direction.value} {trade.remaining_size}/"
                f"{trade.initial_size} @ {trade.entry_price} | now {marker}\n"
                f"   SL {trade.stop_level} TP1 {trade.tp1} TP2 {trade.tp2} TP3 {trade.tp3} "
                f"[{rungs}] id {trade.short_id}"
            )
        return "\n".join(lines) if lines else "No positions are being managed."

    def report_command(self, argument: str) -> str:
        epics = [argument.strip().upper()] if argument.strip() else [
            item.epic for item in self.config.analysis.watchlist
        ]
        if not epics:
            return "No epic given and the watchlist is empty."
        replies: List[str] = []
        for epic in epics:
            try:
                plan = self._plan_for(epic, force=True)
                self._write_report_file(plan)
                replies.append(render_text(plan))
            except Exception as exc:
                replies.append(f"{epic}: report failed -- {exc}")
        return "\n\n".join(replies)

    def plan_command(self, argument: str) -> str:
        epic = argument.strip().upper()
        if not epic:
            return "Usage: /plan <epic>"
        plan = self.store.latest_plan(epic)
        return render_text(plan) if plan else f"No stored plan for {epic}. Try /report {epic}."

    def close_command(self, argument: str) -> str:
        trade = self.store.trade_by_short_id(argument.strip())
        if trade is None:
            return f"No trade matching {argument.strip()!r}."
        decision = Decision(
            kind=DecisionKind.CLOSE_ALL,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:manual_close:{utcnow():%Y%m%d%H%M%S}",
            reason="manual close requested",
            size=trade.remaining_size,
        )
        applied = self.engine.apply(trade, Evaluation(decisions=[decision], blocked=[]))
        self.store.save_trade(trade)
        return "\n".join(applied) if applied else "Close was not accepted; check the logs."

    def breakeven_command(self, argument: str) -> str:
        trade = self.store.trade_by_short_id(argument.strip())
        if trade is None:
            return f"No trade matching {argument.strip()!r}."
        decision = Decision(
            kind=DecisionKind.SET_STOP,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:manual_be:{utcnow():%Y%m%d%H%M%S}",
            reason="manual break-even requested",
            stop_level=trade.entry_price,
        )
        applied = self.engine.apply(trade, Evaluation(decisions=[decision], blocked=[]))
        self.store.save_trade(trade)
        return "\n".join(applied) if applied else "Stop move was not accepted; check the logs."
