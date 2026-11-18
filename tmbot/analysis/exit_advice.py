"""Which target is realistic, and should this deal hold, take profit or tighten?

Advice only.  Nothing here touches the broker; ``manage.advisor`` shows the
result and a person confirms any action.  The existing engine keeps every exit
it already has (broker-side targets, TP3 extension, trailing, the reversal
failsafe) -- this module only says what the evidence suggests.

How it decides, in order, and why each step needs no invented percentage:

1. A confirmed trend reversal (the engine's own detector: enough independent
   signals for enough cycles) while the deal is in profit -> CLOSE.
2. Tested odds, when the history is long enough (``analysis.odds``).  For each
   target still ahead, in units of the risk taken (R):

       expected value of holding to the target
           = P(target first) x R(target)
           + P(stop first)   x R(stop)
           + P(neither)      x R(now)

   and compare with the R already in hand if the deal is closed now.  If no
   target ahead beats taking the profit now, take it: CLOSE (PARTIAL CLOSE in
   the partial-close exit model).  The probabilities are the measured
   frequencies; nothing is rounded up or assumed.
3. The most realistic target is the one with the best expected value -- it is
   not assumed to be TP3.
4. Fading momentum (MACD histogram shrinking, DI spread narrowing) together
   with at least one reversal signal firing, while in profit -> MOVE STOP to an
   ATR-behind-price lock (the same distance the reversal failsafe uses), only if
   it improves the stop and the broker accepts that distance.
5. Otherwise HOLD, with the evidence listed.

Without enough history the odds are ``None`` and steps 2-3 are skipped; the
advice then rests on steps 1, 4 and 5 and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import ExitAdviceConfig, ManagementConfig, ReversalConfig
from ..models import Direction, ManagedTrade, MarketSnapshot, Stage
from .odds import OddsResult
from .reversal import ReversalSignal, detect

HOLD, PARTIAL_CLOSE, MOVE_STOP, CLOSE = "HOLD", "PARTIAL_CLOSE", "MOVE_STOP", "CLOSE"
ACTIONS = (HOLD, PARTIAL_CLOSE, MOVE_STOP, CLOSE)
Reason = Tuple[str, Dict[str, Any]]


@dataclass
class TargetView:
    stage: str
    level: float
    distance_atr: float
    reward_r: float
    samples: int = 0
    p: Optional[float] = None         # reached before the stop, measured
    q: Optional[float] = None         # stop first (a reversal before the target)
    low: Optional[float] = None
    high: Optional[float] = None
    ev_r: Optional[float] = None      # expected value of holding to it, in R

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Advice:
    deal_id: str
    action: str = HOLD
    reasons: List[Reason] = field(default_factory=list)
    targets: List[TargetView] = field(default_factory=list)
    best_stage: Optional[str] = None
    own_stage: Optional[str] = None
    stop_level: Optional[float] = None
    size: Optional[float] = None
    r_now: float = 0.0
    r_stop: float = 0.0
    tested: bool = False              # odds were available
    conditioned: bool = False
    samples: int = 0
    horizon: int = 0
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.__dict__)
        out["targets"] = [t.to_dict() for t in self.targets]
        out["reasons"] = [[key, args] for key, args in self.reasons]
        return out


def stop_now(trade: ManagedTrade) -> float:
    return trade.stop_level if trade.stop_level is not None else trade.sl


def candidate_stages(trade: ManagedTrade, price: float, config: ManagementConfig) -> List[str]:
    """Targets this deal could still end at: those ahead of the price.

    A three-deal leg that is not the runner has exactly one target.  The runner
    and a partial-close deal can end at any rung still ahead, which is what
    makes "TP2 instead of TP3" a real question.
    """
    levels = {"TP1": trade.tp1, "TP2": trade.tp2, "TP3": trade.tp3}
    if config.exit_model == "three_deals" and not trade.is_runner:
        stages = [(trade.leg_target or Stage.TP3).value]
    else:
        stages = ["TP1", "TP2", "TP3"]
        if config.exit_model != "three_deals":
            done = {"TP1": trade.tp1_done, "TP2": trade.tp2_done}
            stages = [s for s in stages if not done.get(s)]
    return [s for s in stages if (levels[s] - price) * trade.direction.sign > 0]


def geometry(
    trade: ManagedTrade, snapshot: MarketSnapshot, config: ManagementConfig
) -> Optional[Tuple[float, Dict[str, float]]]:
    """(stop distance in ATR, {stage: distance in ATR}) from the current price."""
    if snapshot.atr <= 0:
        return None
    price = snapshot.quote.exit_price(trade.direction)
    sign = trade.direction.sign
    stop_distance = (price - stop_now(trade)) * sign
    if stop_distance <= 0:
        return None
    levels = {"TP1": trade.tp1, "TP2": trade.tp2, "TP3": trade.tp3}
    targets = {
        stage: (levels[stage] - price) * sign / snapshot.atr
        for stage in candidate_stages(trade, price, config)
    }
    return stop_distance / snapshot.atr, targets


def _momentum_fading(trade: ManagedTrade, snapshot: MarketSnapshot) -> Tuple[bool, bool]:
    """(MACD histogram shrinking against the trade, DI spread narrowing)."""
    sign = trade.direction.sign
    hist, prev = snapshot.macd_hist, snapshot.macd_hist_prev
    macd = bool(hist is not None and prev is not None and hist * sign < prev * sign)
    plus, minus = snapshot.plus_di, snapshot.minus_di
    plus_prev, minus_prev = snapshot.plus_di_prev, snapshot.minus_di_prev
    di = False
    if None not in (plus, minus, plus_prev, minus_prev):
        di = (plus - minus) * sign < (plus_prev - minus_prev) * sign
    return macd, di


def advise(
    trade: ManagedTrade,
    snapshot: MarketSnapshot,
    odds: Optional[OddsResult],
    config: ManagementConfig,
    reversal_config: ReversalConfig,
    advice_config: ExitAdviceConfig,
    *,
    signal: Optional[ReversalSignal] = None,
) -> Advice:
    """The recommendation for one deal, with the reasons behind it."""
    sign = trade.direction.sign
    price = snapshot.quote.exit_price(trade.direction)
    rules = snapshot.rules
    atr = snapshot.atr
    risk = trade.initial_risk or 0.0
    r_now = trade.r_multiple(price)
    r_stop = (stop_now(trade) - trade.entry_price) * sign / risk if risk else 0.0
    levels = {"TP1": trade.tp1, "TP2": trade.tp2, "TP3": trade.tp3}
    own = (trade.leg_target or Stage.TP3).value if config.exit_model == "three_deals" else None

    advice = Advice(deal_id=trade.deal_id, r_now=round(r_now, 3), r_stop=round(r_stop, 3),
                    own_stage=own)
    if signal is None and reversal_config is not None and reversal_config.enabled:
        signal = detect(trade, snapshot, reversal_config)

    # ---- the targets still ahead, with their tested odds
    for stage in candidate_stages(trade, price, config):
        level = levels[stage]
        view = TargetView(
            stage=stage, level=level,
            distance_atr=round((level - price) * sign / atr, 3) if atr > 0 else 0.0,
            reward_r=round((level - trade.entry_price) * sign / risk, 3) if risk else 0.0,
        )
        reach = odds.reach.get(stage) if odds else None
        if reach is not None:
            view.samples = reach.samples
            if reach.enough and reach.p is not None and reach.q is not None:
                view.p, view.q = round(reach.p, 4), round(reach.q, 4)
                if reach.interval:
                    view.low, view.high = round(reach.interval[0], 4), round(reach.interval[1], 4)
                view.ev_r = round(
                    reach.p * view.reward_r + reach.q * r_stop
                    + (1.0 - reach.p - reach.q) * r_now, 3)
        advice.targets.append(view)
    known = [t for t in advice.targets if t.ev_r is not None]
    advice.tested = bool(known)
    if odds is not None:
        advice.conditioned, advice.samples, advice.horizon = (
            odds.conditioned, odds.samples, odds.horizon)
    best = max(known, key=lambda t: (t.ev_r, -t.distance_atr)) if known else None
    advice.best_stage = best.stage if best else None

    # ---- the evidence, listed whatever is decided
    macd_fading, di_narrowing = _momentum_fading(trade, snapshot)
    agreeing = signal.agreeing if signal else 0
    required = signal.required if signal else 0
    nearest = min((t.distance_atr for t in advice.targets), default=None)
    advice.evidence = {
        "adx": round(snapshot.adx, 1), "strength": snapshot.strength.value,
        "macd_fading": macd_fading, "di_narrowing": di_narrowing,
        "reversal_agreeing": agreeing, "reversal_required": required,
        "reversal_confirmed": bool(signal and signal.confirmed),
        "nearest_target_atr": nearest,
    }
    in_profit = r_now > 0
    reasons = advice.reasons

    def finish(action: str, *items: Reason, **fields: Any) -> Advice:
        advice.action = action
        reasons[:0] = list(items)
        for name, value in fields.items():
            setattr(advice, name, value)
        reasons.append(("evidence", {
            "strength": snapshot.strength.value, "adx": f"{snapshot.adx:.0f}",
            "momentum": "fading" if (macd_fading or di_narrowing) else "steady",
            "agreeing": agreeing, "required": required,
            "nearest": "-" if nearest is None else f"{nearest:.1f}"}))
        if not advice.tested:
            reasons.append(("no_odds", {"samples": odds.samples if odds else 0,
                                        "minimum": advice_config.min_samples}))
        return advice

    # 1. a confirmed reversal, with profit to protect
    if signal is not None and signal.confirmed and in_profit and not trade.reversal_muted:
        return finish(CLOSE, ("close_reversal", {
            "agreeing": agreeing, "required": required, "r": f"{r_now:+.2f}"}))

    # 2. the tested expected value of holding, against what is in hand
    if best is not None and in_profit and best.ev_r <= r_now + 1e-9:
        why = ("close_ev", {"stage": best.stage, "ev": f"{best.ev_r:+.2f}",
                            "now": f"{r_now:+.2f}", "samples": advice.samples})
        partial = _partial_size(trade, rules, config, advice_config)
        if partial is not None:
            return finish(PARTIAL_CLOSE, why, size=partial)
        return finish(CLOSE, why)

    # 3. the realistic target, when it is not the farthest one
    if best is not None:
        far = max(advice.targets, key=lambda t: t.distance_atr)
        if best.stage != far.stage:
            reasons.append(("realistic_target", {
                "best": best.stage, "ev": f"{best.ev_r:+.2f}", "far": far.stage,
                "far_ev": "-" if far.ev_r is None else f"{far.ev_r:+.2f}"}))

    # 4. momentum fading with a reversal signal firing, while in profit
    if in_profit and (macd_fading or di_narrowing) and agreeing >= 1 and atr > 0:
        level = price - sign * reversal_config.tighten_atr * atr
        level = rules.round_price(level) if rules else round(level, 5)
        minimum = config.min_stop_improvement_atr * atr
        improves = (level - stop_now(trade)) * sign >= minimum
        margin = rules.stop_buffer(price) if rules else 0.0
        valid = (price - level) * sign >= margin
        if improves and valid:
            return finish(MOVE_STOP, ("move_stop", {
                "level": level, "atr": reversal_config.tighten_atr,
                "agreeing": agreeing, "required": required}), stop_level=level)
        reasons.append(("stop_not_improved", {"level": level}))

    return finish(HOLD, ("hold", {"r": f"{r_now:+.2f}"}))


def _partial_size(
    trade: ManagedTrade, rules: Any, config: ManagementConfig, advice_config: ExitAdviceConfig
) -> Optional[float]:
    """Size for a PARTIAL CLOSE, or None to close the whole deal instead.

    Only in the partial-close exit model (a three-deal leg is closed whole), and
    only if both the part and what is left stay at or above the broker minimum.
    """
    if config.exit_model == "three_deals":
        return None
    size = trade.remaining_size * advice_config.partial_fraction
    size = rules.round_size(size) if rules else round(size, 4)
    minimum = rules.min_deal_size if rules else 0.0
    left = round(trade.remaining_size - size, 6)
    if size <= 0 or (minimum and (size < minimum or left < minimum)):
        return None
    return size
