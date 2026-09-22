"""Pure trade-management decisions.

``evaluate`` takes a trade plus a market snapshot and returns the ordered list
of broker actions that should follow.  It touches no network and mutates
nothing, which is what makes the whole ladder testable: every scenario in
``tests/test_rules.py`` is a snapshot in, a decision list out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..analysis.reversal import ReversalSignal, detect, recovery_stop
from ..config import ManagementConfig, ReversalConfig
from ..models import (
    Direction,
    ManagedTrade,
    MarketRules,
    MarketSnapshot,
    Stage,
    TrendStrength,
)


class DecisionKind(str, Enum):
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    CLOSE_ALL = "CLOSE_ALL"
    SET_STOP = "SET_STOP"
    SET_TARGET = "SET_TARGET"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    deal_id: str
    key: str            # idempotency key -- the journal is keyed on this
    reason: str         # English, for the log and the action journal
    # Translation key and arguments for the same reason, rendered per language
    # at the notification layer. `reason` stays English so the audit trail and
    # the logs are searchable whatever the display language.
    reason_key: str = ""
    reason_args: Dict[str, Any] = field(default_factory=dict)
    size: Optional[float] = None
    stop_level: Optional[float] = None
    profit_level: Optional[float] = None
    stage: Optional[Stage] = None

    def describe(self) -> str:
        if self.kind is DecisionKind.PARTIAL_CLOSE:
            return f"close {self.size} ({self.stage.value if self.stage else '?'}): {self.reason}"
        if self.kind is DecisionKind.CLOSE_ALL:
            return f"close remaining {self.size}: {self.reason}"
        if self.kind is DecisionKind.SET_STOP:
            return f"stop -> {self.stop_level}: {self.reason}"
        return f"target -> {self.profit_level}: {self.reason}"


@dataclass(frozen=True)
class Blocked:
    """Something we wanted to do but could not, with the reason why."""

    kind: DecisionKind
    reason: str


@dataclass
class Evaluation:
    decisions: List[Decision]
    blocked: List[Blocked]
    reversal: Optional["ReversalSignal"] = None

    def __iter__(self):
        return iter(self.decisions)


def _stage_level(trade: ManagedTrade, stage: str) -> Optional[float]:
    return {"TP1": trade.tp1, "TP2": trade.tp2, "TP3": trade.tp3}.get(stage.upper())


def _stage_reached(trade: ManagedTrade, stage: str, price: float) -> bool:
    if stage.upper() == "ENTRY":
        return True
    if stage.upper() == "NEVER":
        return False
    level = _stage_level(trade, stage)
    return level is not None and trade.direction.is_beyond(price, level)


def trail_multiplier(strength: TrendStrength, config: ManagementConfig) -> Optional[float]:
    """ATR multiple for the chandelier, or None when we should not trail at all."""
    if strength is TrendStrength.STRONG:
        return config.trail_k_strong
    if strength is TrendStrength.MODERATE:
        return config.trail_k_moderate
    return config.trail_k_moderate if config.trail_when_weak else None


def chandelier_stop(
    trade: ManagedTrade,
    snapshot: MarketSnapshot,
    multiplier: float,
    config: ManagementConfig,
) -> Optional[float]:
    """Chandelier level, floored by the last confirmed swing.

    For a long: ``highest_high_since_entry - k*ATR``, but never looser than the
    most recent confirmed swing low less a small buffer.  Taking the tighter of
    the two keeps the stop behind real structure while ATR keeps it out of noise.
    """
    if snapshot.atr <= 0 or trade.best_price is None:
        return None
    sign = trade.direction.sign
    chandelier = trade.best_price - sign * multiplier * snapshot.atr

    buffer = config.structure_buffer_atr * snapshot.atr
    structure = snapshot.swing_low if trade.direction is Direction.BUY else snapshot.swing_high
    if structure is None:
        return chandelier
    structural = structure - sign * buffer
    return max(chandelier, structural) if trade.direction is Direction.BUY else min(
        chandelier, structural
    )


def _valid_stop(
    trade: ManagedTrade,
    level: float,
    price: float,
    rules: Optional[MarketRules],
) -> bool:
    """A stop must stay on the losing side of the current price by the broker's margin."""
    margin = rules.stop_buffer(price) if rules else 0.0
    if trade.direction is Direction.BUY:
        return level <= price - margin
    return level >= price + margin


def _improves(trade: ManagedTrade, level: float, minimum: float) -> bool:
    """Stops only ever ratchet toward profit, and only by a meaningful step."""
    current = trade.stop_level if trade.stop_level is not None else None
    if current is None:
        return True
    if trade.direction is Direction.BUY:
        return level - current >= minimum
    return current - level >= minimum


def evaluate(
    trade: ManagedTrade,
    snapshot: MarketSnapshot,
    config: ManagementConfig,
    reversal_config: Optional[ReversalConfig] = None,
) -> Evaluation:
    """Decide what should happen to ``trade`` right now."""
    decisions: List[Decision] = []
    blocked: List[Blocked] = []
    rules = snapshot.rules
    price = snapshot.quote.exit_price(trade.direction)
    direction = trade.direction

    def round_price(value: float) -> float:
        return rules.round_price(value) if rules else round(value, 5)

    def round_size(value: float) -> float:
        return rules.round_size(value) if rules else round(value, 4)

    minimum_size = rules.min_deal_size if rules else 0.0
    remaining = trade.remaining_size

    three_deals = config.exit_model == "three_deals"

    # ---------------------------------------------------------------- leg exit
    # Three-deal mode: this deal has exactly one target and is closed whole at
    # it.  The TP3 leg is left to the shared final-target block below so it can
    # still be extended in a strong trend.
    if three_deals:
        stage = (trade.leg_target or Stage.TP3)
        level = _stage_level(trade, stage.value)
        if (
            stage is not Stage.TP3
            and level is not None
            and remaining > 0
            and direction.is_beyond(price, level)
        ):
            decisions.append(Decision(
                kind=DecisionKind.CLOSE_ALL,
                deal_id=trade.deal_id,
                key=f"{trade.deal_id}:leg_close:{stage.value}",
                reason=(
                    f"leg {trade.leg_index + 1} target {stage.value} {level} "
                    f"reached at {price}"
                ),
                reason_key="reason.leg_target",
                reason_args={"index": trade.leg_index + 1, "stage": stage.value,
                             "level": level, "price": price},
                size=remaining,
                stage=stage,
            ))
            remaining = 0.0

    # ---------------------------------------------------------------- ladder
    done_flags = {"TP1": trade.tp1_done, "TP2": trade.tp2_done}
    for step in (() if three_deals else config.ladder):
        stage = step.stage.upper()
        level = _stage_level(trade, stage)
        if level is None or done_flags.get(stage):
            continue
        if not direction.is_beyond(price, level):
            continue

        size = round_size(trade.initial_size * step.fraction)
        if size <= 0 or (minimum_size and size < minimum_size):
            blocked.append(Blocked(
                DecisionKind.PARTIAL_CLOSE,
                f"{stage}: {step.fraction:.0%} of {trade.initial_size} rounds to {size}, "
                f"below the {minimum_size} minimum deal size",
            ))
            done_flags[stage] = True  # do not retry every cycle
            continue

        size = min(size, remaining)
        leftover = round(remaining - size, 6)
        if leftover > 0 and minimum_size and leftover < minimum_size:
            if config.on_indivisible_size == "close_all":
                decisions.append(Decision(
                    kind=DecisionKind.CLOSE_ALL,
                    deal_id=trade.deal_id,
                    key=f"{trade.deal_id}:close:{stage}",
                    reason=(
                        f"{stage} hit and the remainder would be below the "
                        "minimum deal size"
                    ),
                    reason_key="reason.indivisible",
                    reason_args={"stage": stage},
                    size=remaining,
                    stage=Stage(stage),
                ))
                remaining = 0.0
                break
            blocked.append(Blocked(
                DecisionKind.PARTIAL_CLOSE,
                f"{stage}: closing {size} would leave {leftover}, below the "
                f"{minimum_size} minimum -- holding full size (on_indivisible_size=hold)",
            ))
            done_flags[stage] = True
            continue

        decisions.append(Decision(
            kind=DecisionKind.PARTIAL_CLOSE,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:partial:{stage}",
            reason=f"{stage} {level} reached at {price}",
            reason_key="reason.stage_reached",
            reason_args={"stage": stage, "level": level, "price": price},
            size=size,
            stage=Stage(stage),
        ))
        remaining = round(remaining - size, 6)

    # ---------------------------------------------------------------- stop candidates
    # Break-even, trailing and reversal can all want the stop moved in the same
    # cycle. Each proposes a level; the tightest valid one wins and a single
    # modification goes to the broker. Letting each overwrite the last sends
    # three requests and lands on whichever ran last, not whichever is best.
    stop_candidates: List[tuple] = []

    # Keyed off price reaching the stage, not off the partial having succeeded:
    # if the partial close fails we still want the trade risk-free.
    breakeven_due = not trade.breakeven_done and _stage_reached(
        trade, config.breakeven_stage, price
    )
    if breakeven_due:
        level = round_price(
            trade.entry_price + direction.sign * config.breakeven_offset_r * trade.initial_risk
        )
        stop_candidates.append((
            level, "breakeven", "reason.breakeven",
            {"stage": config.breakeven_stage, "level": level},
            f"{config.breakeven_stage} reached -- stop to entry {level}",
        ))

    # ---------------------------------------------------------------- final target
    # In three-deal mode every other leg has its own exit and must not be
    # dragged to TP3 or trailed out of its target.
    runner = trade.is_runner if three_deals else True
    beyond_tp3 = runner and direction.is_beyond(price, trade.tp3)
    can_extend = (
        runner
        and config.extend_tp3
        and snapshot.strength is TrendStrength.STRONG
        and trade.tp3_extensions < config.tp3_max_extensions
        and snapshot.atr > 0
    )
    near_tp3 = (
        runner
        and snapshot.atr > 0
        and abs(trade.tp3 - price) <= config.tp3_extension_trigger_atr * snapshot.atr
    )

    if (beyond_tp3 or near_tp3) and can_extend:
        extended = round_price(
            trade.tp3 + direction.sign * config.tp3_extension_atr * snapshot.atr
        )
        decisions.append(Decision(
            kind=DecisionKind.SET_TARGET,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:extend:{trade.tp3_extensions + 1}",
            reason=(
                f"strong trend into TP3 {trade.tp3} -- extending to {extended} "
                f"(extension {trade.tp3_extensions + 1}/{config.tp3_max_extensions})"
            ),
            reason_key="reason.extend",
            reason_args={"stage": "TP3", "level": trade.tp3, "new": extended,
                         "count": trade.tp3_extensions + 1,
                         "limit": config.tp3_max_extensions},
            profit_level=extended,
            stage=Stage.TP3,
        ))
    elif beyond_tp3 and remaining > 0:
        decisions.append(Decision(
            kind=DecisionKind.CLOSE_ALL,
            deal_id=trade.deal_id,
            key=f"{trade.deal_id}:close:TP3",
            reason=f"TP3 {trade.tp3} reached at {price}",
            reason_key="reason.stage_reached",
            reason_args={"stage": "TP3", "level": trade.tp3, "price": price},
            size=remaining,
            stage=Stage.TP3,
        ))
        remaining = 0.0

    # ---------------------------------------------------------------- trailing stop
    if remaining > 0 and runner and _stage_reached(trade, config.trail_after_stage, price):
        multiplier = trail_multiplier(snapshot.strength, config)
        if multiplier is None:
            blocked.append(Blocked(
                DecisionKind.SET_STOP,
                f"trend is {snapshot.strength.value} -- holding the stop where it is",
            ))
        else:
            candidate = chandelier_stop(trade, snapshot, multiplier, config)
            if candidate is not None:
                # Once risk-free, never give the entry back.
                if trade.breakeven_done or breakeven_due:
                    candidate = (
                        max(candidate, trade.entry_price) if direction is Direction.BUY
                        else min(candidate, trade.entry_price)
                    )
                candidate = round_price(candidate)
                stop_candidates.append((
                    candidate, f"trail:{candidate}", "reason.trail",
                    {"strength": snapshot.strength.value, "k": multiplier,
                     "best": trade.best_price, "atr": f"{snapshot.atr:.5f}",
                     "level": candidate},
                    (f"{snapshot.strength.value} trend, k={multiplier}, "
                     f"best {trade.best_price}, ATR {snapshot.atr:.5f} -> stop {candidate}"),
                ))

    # ---------------------------------------------------------------- reversal failsafe
    signal: Optional[ReversalSignal] = None
    if reversal_config is not None and reversal_config.enabled and remaining > 0:
        signal = detect(trade, snapshot, reversal_config)
        actionable = (
            signal.confirmed
            and not trade.reversal_muted
            and trade.r_multiple(price) >= reversal_config.min_r_to_act
        )
        if actionable and reversal_config.action == "close":
            decisions.append(Decision(
                kind=DecisionKind.CLOSE_ALL,
                deal_id=trade.deal_id,
                key=f"{trade.deal_id}:reversal_close:{signal.streak}",
                reason=f"trend reversed against the position: {signal.summary()}",
                reason_key="reason.reversal_close",
                reason_args={"detail": signal.summary()},
                size=remaining,
            ))
            remaining = 0.0
            stop_candidates.clear()
        elif actionable and reversal_config.action == "tighten":
            candidate = recovery_stop(trade, snapshot, reversal_config)
            if candidate is not None:
                candidate = round_price(candidate)
                stop_candidates.append((
                    candidate, f"reversal:{candidate}", "reason.reversal_tighten",
                    {"detail": signal.summary(), "level": candidate},
                    f"reversal ({signal.summary()}) -- stop tightened to {candidate}",
                ))
        elif signal.confirmed and not trade.reversal_muted:
            blocked.append(Blocked(
                DecisionKind.SET_STOP,
                f"reversal confirmed but the trade is only "
                f"{trade.r_multiple(price):+.2f}R -- alerting instead of acting",
            ))

    # ---------------------------------------------------------------- one stop wins
    # Guarded on the size the trade *had*, not what is left after a close
    # decided above: if that close is rejected the position is still open, and
    # the stop is the only thing protecting it.
    if stop_candidates and trade.remaining_size > 0:
        # Tightest first, then fall down the list. Taking only the best and
        # giving up when it is invalid would drop a perfectly good break-even
        # because a trailing level happened to sit inside the spread.
        ranked = sorted(
            stop_candidates, key=lambda item: item[0],
            reverse=direction is Direction.BUY,
        )
        minimum_step = config.min_stop_improvement_atr * snapshot.atr
        rejected: List[str] = []
        for level, suffix, reason_key, reason_args, english in ranked:
            if not _improves(trade, level, minimum_step):
                continue  # the stop is already at least this good
            if not _valid_stop(trade, level, price, rules):
                rejected.append(
                    f"stop {level} is inside the broker's minimum distance from {price}"
                )
                continue
            # First in the list: protection lands before any close is tried.
            decisions.insert(0, Decision(
                kind=DecisionKind.SET_STOP,
                deal_id=trade.deal_id,
                key=f"{trade.deal_id}:{suffix}",
                reason=english,
                reason_key=reason_key,
                reason_args=reason_args,
                stop_level=level,
            ))
            break
        else:
            for reason in rejected:
                blocked.append(Blocked(DecisionKind.SET_STOP, reason))

    return Evaluation(decisions=decisions, blocked=blocked, reversal=signal)
