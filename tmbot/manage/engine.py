"""Turns decisions into broker calls, exactly once.

Every mutation is claimed in the action journal before it is sent and released
or completed after.  A retryable failure releases the claim so the next cycle
tries again; a permanent rejection marks it failed and shouts, because a stop
that did not move is risk the user thinks is gone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..broker.base import BrokerAdapter, PartialCloseStrategy
from ..config import Config
from ..errors import NotSupportedError, PermanentError, RetryableError
from ..models import BrokerPosition, Direction, Fill, ManagedTrade, TradeStatus, utcnow
from ..i18n import Translator
from ..notify.base import Notifier
from ..store import Store
from .rules import Decision, DecisionKind, Evaluation

log = logging.getLogger(__name__)

# Allow for broker-side rounding when checking a partial close landed.
SIZE_TOLERANCE = 1e-4


@dataclass
class TradeEngine:
    broker: BrokerAdapter
    store: Store
    config: Config
    notifier: Notifier
    t: Translator = field(default_factory=Translator)

    # Reason arguments that hold a stored enum value rather than a number.
    # rules.py is pure and has no translator, so they are converted here, at
    # the point the text is rendered.
    TERM_ARGS = {"strength": "strength", "direction": "direction", "bias": "bias"}

    def _localise(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            name: self.t.term(self.TERM_ARGS[name], value) if name in self.TERM_ARGS
            else value
            for name, value in args.items()
        }

    def describe(self, decision: Decision) -> str:
        """Render a decision for the user, in their language.

        ``Decision.describe`` stays English for the log and the action journal;
        this is the display form.
        """
        stage = decision.stage.value if decision.stage else "?"
        reason = (
            self.t(decision.reason_key, **self._localise(decision.reason_args))
            if decision.reason_key else decision.reason
        )
        if decision.kind is DecisionKind.PARTIAL_CLOSE:
            return self.t("action.partial_close", size=decision.size,
                          stage=stage, reason=reason)
        if decision.kind is DecisionKind.CLOSE_ALL:
            return self.t("action.close_all", size=decision.size, reason=reason)
        if decision.kind is DecisionKind.SET_STOP:
            return self.t("action.set_stop", level=decision.stop_level, reason=reason)
        return self.t("action.set_target", level=decision.profit_level, reason=reason)

    # ------------------------------------------------------------------ sync

    def resync(self, trade: ManagedTrade) -> Optional[BrokerPosition]:
        """Refresh ``trade`` from the broker; None means the position is gone.

        A netting-offset partial close can hand the remainder a new deal id, so
        fall back to matching on epic + direction before declaring the position
        closed.
        """
        position = self.broker.position(trade.deal_id)
        if position is None:
            candidates = [
                item for item in self.broker.positions()
                if item.epic == trade.epic and item.direction is trade.direction
            ]
            if len(candidates) == 1 and candidates[0].deal_id not in self.store.known_deal_ids():
                position = candidates[0]
                log.info(
                    "trade %s re-mapped to new deal id %s after a partial close",
                    trade.deal_id, position.deal_id,
                )
                trade.note = f"re-mapped from {trade.deal_id}"
                trade.deal_id = position.deal_id
        if position is None:
            return None
        trade.remaining_size = position.size
        trade.stop_level = position.stop_level
        return position

    # ------------------------------------------------------------------ apply

    def apply(self, trade: ManagedTrade, evaluation: Evaluation) -> List[str]:
        """Execute every decision in order; returns human-readable outcomes."""
        applied: List[str] = []
        for note in evaluation.blocked:
            log.debug("trade %s blocked: %s", trade.short_id, note.reason)

        for decision in evaluation.decisions:
            if not self.store.begin_action(
                decision.key, trade.deal_id, decision.kind.value, decision.reason
            ):
                log.debug("skipping %s: already claimed", decision.key)
                continue
            try:
                message = self._execute(trade, decision)
            except RetryableError as exc:
                # Leave nothing behind: the next cycle re-evaluates from scratch.
                self.store.release_action(decision.key)
                log.warning("%s deferred: %s", decision.key, exc)
                break
            except (PermanentError, NotSupportedError) as exc:
                self.store.fail_action(decision.key, str(exc))
                trade.status = TradeStatus.ERROR
                self.store.save_trade(trade)
                self.store.log_event(
                    "action_failed", f"{decision.describe()} -> {exc}",
                    deal_id=trade.deal_id, epic=trade.epic,
                )
                self.notifier.send(
                    self.t("action.failed", epic=trade.epic, id=trade.short_id,
                           action=self.describe(decision), error=exc),
                    level="error",
                )
                break
            else:
                self.store.complete_action(decision.key, message)
                self.store.log_event(
                    decision.kind.value, decision.describe(),
                    deal_id=trade.deal_id, epic=trade.epic,
                )
                applied.append(self.describe(decision))

        if applied:
            self.store.save_trade(trade)
        return applied

    # ------------------------------------------------------------------ execution

    def _execute(self, trade: ManagedTrade, decision: Decision) -> str:
        if self.config.dry_run:
            log.info("[dry-run] %s %s", trade.epic, decision.describe())
            self._record_local_state(trade, decision)
            return "dry-run"

        before = trade.remaining_size
        if decision.kind is DecisionKind.SET_STOP:
            reference = self.broker.modify_position(
                trade.deal_id, stop_level=decision.stop_level
            )
        elif decision.kind is DecisionKind.SET_TARGET:
            reference = self.broker.modify_position(
                trade.deal_id, profit_level=decision.profit_level
            )
        elif decision.kind in (DecisionKind.PARTIAL_CLOSE, DecisionKind.CLOSE_ALL):
            reference = self._close(trade, decision)
        else:  # pragma: no cover - the enum is exhaustive
            raise NotSupportedError(f"unhandled decision kind {decision.kind}")

        self._record_local_state(trade, decision, before=before, reference=reference)
        return reference

    @staticmethod
    def _fill_stage(decision: Decision) -> str:
        """Label the exit so the journal can group by what caused it."""
        if decision.stage is not None:
            return decision.stage.value
        if ":reversal" in decision.key:
            return "REVERSAL"
        if ":manual" in decision.key:
            return "MANUAL"
        return "CLOSE"

    def _booked(self, reference: Optional[str]) -> Optional[Dict[str, Any]]:
        """What the broker actually booked for this order, if it can say."""
        if not reference or self.config.dry_run:
            return None
        try:
            return self.broker.deal_details(reference)
        except Exception as exc:  # the journal must not cost us the exit
            log.warning("could not read the booked deal %s (%s)", reference, exc)
            return None

    def _record_fill(
        self, trade: ManagedTrade, decision: Decision, reference: Optional[str] = None
    ) -> None:
        """Log what this exit earned, in R.

        Without this there is no way to answer "is this working?" -- and the
        answer only exists if it is captured as the trade closes. There is no
        reconstructing it afterwards.
        """
        if decision.size is None or decision.exit_price is None or not trade.initial_size:
            return
        stage = self._fill_stage(decision)
        if self.store.has_fill(trade.deal_id, stage):
            return  # already recorded; a replay must not double-count
        # The quote we decided on is only the price we hoped for; the broker's
        # booked deal is what actually happened, slippage and all.
        booked = self._booked(reference) or {}
        size = booked.get("volume") or decision.size
        fill = Fill(
            deal_id=trade.deal_id, epic=trade.epic, ts=utcnow(), stage=stage,
            size=size, price=booked.get("price", decision.exit_price),
            entry_price=trade.entry_price, direction=trade.direction,
            initial_risk=trade.initial_risk,
            fraction=min(1.0, size / trade.initial_size),
            profit=booked.get("profit"), commission=booked.get("commission"),
            swap=booked.get("swap"), broker_ref=str(booked.get("refs") or reference or ""),
        )
        self.store.record_fill(fill)
        trade.realised = round(trade.realised + fill.r_multiple, 6)

    def _record_local_state(
        self,
        trade: ManagedTrade,
        decision: Decision,
        *,
        before: Optional[float] = None,
        reference: Optional[str] = None,
    ) -> None:
        """Advance our own flags so the ladder cannot fire the same rung twice.

        ``before`` is the size held before the order went out. A partial close
        re-reads the position from the broker to verify it; when that already
        updated the size, subtracting the partial again would count it twice.
        """
        if decision.kind in (DecisionKind.PARTIAL_CLOSE, DecisionKind.CLOSE_ALL):
            self._record_fill(trade, decision, reference)
        if decision.kind is DecisionKind.PARTIAL_CLOSE and decision.stage:
            if decision.stage.value == "TP1":
                trade.tp1_done = True
            elif decision.stage.value == "TP2":
                trade.tp2_done = True
            already_synced = before is not None and abs(trade.remaining_size - before) > SIZE_TOLERANCE
            if not already_synced:
                trade.remaining_size = max(
                    0.0, round(trade.remaining_size - (decision.size or 0.0), 6)
                )
        elif decision.kind is DecisionKind.CLOSE_ALL:
            trade.remaining_size = 0.0
            trade.status = TradeStatus.CLOSED
            trade.closed_at = utcnow()
        elif decision.kind is DecisionKind.SET_STOP and decision.stop_level is not None:
            trade.stop_level = decision.stop_level
            trade.sl = decision.stop_level
            # Any stop at or beyond entry means the trade is risk-free.
            risk_free = (
                decision.stop_level >= trade.entry_price if trade.direction is Direction.BUY
                else decision.stop_level <= trade.entry_price
            )
            if risk_free:
                trade.breakeven_done = True
            if decision.key.startswith(f"{trade.deal_id}:trail:"):
                trade.trailing_active = True
        elif (
            decision.kind is DecisionKind.SET_TARGET
            and decision.profit_level is not None
            and ":extend:" in decision.key
        ):
            # Only an extension moves TP3. Placing a leg's own target at
            # adoption (TP1 on leg 1, say) must not rewrite TP3 or use up one
            # of the extensions.
            trade.tp3 = decision.profit_level
            trade.tp3_extensions += 1

    def _close(self, trade: ManagedTrade, decision: Decision) -> str:
        size = decision.size
        if size is None or size <= 0:
            raise PermanentError(f"refusing to close a non-positive size ({size})")

        full_close = decision.kind is DecisionKind.CLOSE_ALL or size >= trade.remaining_size
        if full_close:
            return self.broker.close_position(trade.deal_id)

        strategy = getattr(self.broker, "partial_close_strategy", PartialCloseStrategy.UNSUPPORTED)
        if strategy is PartialCloseStrategy.DELETE_WITH_SIZE:
            reference = self.broker.close_position(trade.deal_id, size=size)
        elif strategy is PartialCloseStrategy.NETTING_OFFSET:
            reference = self.broker.open_position(
                trade.epic, trade.direction.opposite, size
            )
        else:
            raise NotSupportedError(
                "this account has no safe partial-close path; "
                "see the startup capability probe"
            )
        self._verify_partial(trade, size)
        return reference

    def _verify_partial(self, trade: ManagedTrade, size: float) -> None:
        """Confirm the position actually shrank by the amount we asked for.

        A broker that ignores the size parameter closes everything; catching
        that here turns a silent, expensive surprise into a loud alert.
        """
        expected = round(trade.remaining_size - size, 6)
        try:
            position = self.resync(trade)
        except RetryableError as exc:
            log.warning("could not verify partial close on %s: %s", trade.short_id, exc)
            return

        if position is None:
            if expected > SIZE_TOLERANCE:
                self.notifier.send(
                    self.t("action.overclosed", epic=trade.epic, id=trade.short_id,
                           size=size, expected=expected),
                    level="error",
                )
                self.broker.partial_close_strategy = PartialCloseStrategy.UNSUPPORTED
            return

        if abs(position.size - expected) > SIZE_TOLERANCE:
            self.notifier.send(
                self.t("action.size_mismatch", epic=trade.epic, id=trade.short_id,
                       size=size, actual=position.size, expected=expected),
                level="warn",
            )
