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

from ..config import ManagementConfig
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

    # ---------------------------------------------------------------- break-even
    # Deliberately keyed off price, not off the partial having succeeded: if the
    # partial close fails we still want the trade risk-free.
    if not trade.breakeven_done and _stage_reached(trade, config.breakeven_stage, price):
        level = round_price(
            trade.entry_price + direction.sign * config.breakeven_offset_r * trade.initial_risk
        )
        if _valid_stop(trade, level, price, rules):
            decisions.append(Decision(
                kind=DecisionKind.SET_STOP,
                deal_id=trade.deal_id,
                key=f"{trade.deal_id}:breakeven",
                reason=f"{config.breakeven_stage} reached -- stop to entry {level}",
                reason_key="reason.breakeven",
                reason_args={"stage": config.breakeven_stage, "level": level},
                stop_level=level,
            ))
        else:
            blocked.append(Blocked(
                DecisionKind.SET_STOP,
                f"break-even stop {level} is inside the broker's minimum distance from {price}",
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
                if trade.breakeven_done or any(
                    decision.key.endswith(":breakeven") for decision in decisions
                ):
                    candidate = (
                        max(candidate, trade.entry_price) if direction is Direction.BUY
                        else min(candidate, trade.entry_price)
                    )
                candidate = round_price(candidate)
                minimum_step = config.min_stop_improvement_atr * snapshot.atr
                pending_stop = next(
                    (d.stop_level for d in decisions if d.kind is DecisionKind.SET_STOP), None
                )
                improves = _improves(trade, candidate, minimum_step) and (
                    pending_stop is None
                    or (candidate > pending_stop if direction is Direction.BUY
                        else candidate < pending_stop)
                )
                if not improves:
                    pass  # nothing to do: the stop is already at least this good
                elif not _valid_stop(trade, candidate, price, rules):
                    blocked.append(Blocked(
                        DecisionKind.SET_STOP,
                        f"trailing stop {candidate} is inside the broker's minimum "
                        f"distance from {price}",
                    ))
                else:
                    decisions = [
                        d for d in decisions
                        if not (d.kind is DecisionKind.SET_STOP and d.key.endswith(":breakeven"))
                    ] + [Decision(
                        kind=DecisionKind.SET_STOP,
                        deal_id=trade.deal_id,
                        key=f"{trade.deal_id}:trail:{candidate}",
                        reason=(
                            f"{snapshot.strength.value} trend, k={multiplier}, "
                            f"best {trade.best_price}, ATR {snapshot.atr:.5f} -> stop {candidate}"
                        ),
                        reason_key="reason.trail",
                        reason_args={"strength": snapshot.strength.value,
                                     "k": multiplier, "best": trade.best_price,
                                     "atr": f"{snapshot.atr:.5f}", "level": candidate},
                        stop_level=candidate,
                    )]

    return Evaluation(decisions=decisions, blocked=blocked)
