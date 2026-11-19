"""The three-part analysis: Technical, Fundamental and Overall (SHADOW ONLY).

Every result is a direction (bullish / bearish / neutral) and a 0-100 strength,
with the reasons behind it.  Nothing here is read by any decision: the bias,
the signal strength, the quality verdict, the automatic-mode gates and the
orders all keep using the existing scoring.  ``compute`` is a pure function of
what the report already has; it reads nothing and changes nothing.

Technical (T)
    direction  the technical bias (direction score beyond +-20)
    strength   the existing technical strength (0.7*|direction score| +
               trend/momentum agreement +-15 + 0.375*min(ADX, 40)); nothing
               about news is in it.

Fundamental (F)  -- only from headlines that are fresh enough to use
    direction  the news read (BULLISH / BEARISH / NEUTRAL)
    strength   the read's own confidence (0-100)
    quality    reliability x freshness x coverage, each 0..1 (see below)
    If there is no usable news the fundamental result is UNAVAILABLE: no
    direction, no strength, and it takes no part in the overall result.
    Missing data is never read as bullish, bearish or neutral.

Overall (S)
    technical only   when the fundamental result is unavailable: S = T and the
                     direction is the technical direction. Said so explicitly.
    otherwise        weights wT = 0.70 and wF = 0.30 x quality (so poor, stale
                     or thin news counts for less), normalised to sum to 1.
                     direction score  Dc = wT*Dt + wF*Df  (each signed, +-100),
                                      direction beyond +-20, as for technicals
                     agree       both directional, same way:
                                 S = wT*T + wF*F
                     contradict  both directional, opposite ways:
                                 S = | wT*(+-T) + wF*(-+F) |  -- the opposing
                                 evidence cancels part of the strength
                     partial     one side neutral: if only the news is neutral,
                                 S = T x (1 - 0.15*quality)  (unconfirmed); if
                                 only the technicals are neutral, S = wF*F
    It also says which analysis is stronger and how reliable the combination is.

The constants below are provisional: they are what the shadow data is meant to
calibrate, and they are printed with every result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..models import Bias
from . import fundamental as fundamental_module

VERSION = 1

TECH_WEIGHT = 0.70
FUND_WEIGHT = 0.30
DIRECTION_THRESHOLD = 20.0        # the same +-20 the technical bias has always used
FRESH_HOURS = 6.0                 # news this young counts in full
RELIABILITY = {"claude": 1.0, "lexicon": 0.6}   # keyword reading is cruder than the model
FULL_COVERAGE_ITEMS = 3           # this many usable items = full coverage
UNCONFIRMED_DISCOUNT = 0.15       # technical read the news is neutral about
BALANCED_GAP = 10.0               # strengths this close are called balanced
CONFLICT_MIN = 0.25               # a factor must be this strong to be called a conflict

PARAMS = {
    "tech_weight": TECH_WEIGHT, "fund_weight": FUND_WEIGHT,
    "direction_threshold": DIRECTION_THRESHOLD, "fresh_hours": FRESH_HOURS,
    "reliability": RELIABILITY, "full_coverage_items": FULL_COVERAGE_ITEMS,
    "unconfirmed_discount": UNCONFIRMED_DISCOUNT,
}


def _reason(key: str, **args: Any) -> Dict[str, Any]:
    return {"key": key, "args": args}


def _name(bias: Bias) -> str:
    return bias.value


# ------------------------------------------------------------------ technical

def technical_result(technical: Any) -> Dict[str, Any]:
    """Direction, strength and reasons from the technical read alone."""
    direction = technical.bias
    sign = 1 if direction is Bias.BULLISH else -1 if direction is Bias.BEARISH else 0
    ranked = sorted(technical.factors, key=lambda f: abs(f.value * f.weight), reverse=True)
    supports: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    if sign:
        for factor in ranked:
            if factor.name == "range_position":
                continue            # location, not direction (entry quality uses it)
            if factor.value * sign > 0 and len(supports) < 3:
                supports.append(_reason("support", factor=factor.name, detail=factor.detail))
            elif factor.value * sign < 0 and abs(factor.value) >= CONFLICT_MIN and len(conflicts) < 3:
                conflicts.append(_reason("conflict", factor=factor.name, detail=factor.detail))
    else:
        supports.append(_reason("no_direction", score=round(technical.score, 1)))
    weaknesses: List[Dict[str, Any]] = []
    trend = technical.categories.get("trend", 0.0)
    push = technical.categories.get("momentum", 0.0)
    if sign and trend * push < 0:
        weaknesses.append(_reason("trend_momentum_split"))
    if technical.adx < 20:
        weaknesses.append(_reason("weak_trend", adx=round(technical.adx, 1)))
    if sign > 0 and technical.rsi >= 70 or sign < 0 and technical.rsi <= 30:
        weaknesses.append(_reason("stretched", rsi=round(technical.rsi, 1)))
    return {
        "direction": _name(direction),
        "strength": round(technical.confidence, 1),
        "score": round(technical.score, 1),
        "adx": round(technical.adx, 1),
        "supports": supports, "conflicts": conflicts, "weaknesses": weaknesses,
    }


# ---------------------------------------------------------------- fundamental

def _age_hours(raw: Any, now: datetime) -> Optional[float]:
    """Hours since a headline's timestamp (ISO text or epoch seconds), or None."""
    if raw in (None, ""):
        return None
    try:
        if isinstance(raw, (int, float)):
            moment = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        else:
            text = str(raw).strip().replace("Z", "+00:00")
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return max(0.0, (now - moment).total_seconds() / 3600.0)


def _freshness(age: float, lookback: float) -> float:
    """1.0 up to FRESH_HOURS old, falling in a straight line to 0 at the look-back."""
    if age <= FRESH_HOURS:
        return 1.0
    if age >= lookback:
        return 0.0
    return (lookback - age) / (lookback - FRESH_HOURS)


def fundamental_result(
    fundamental: Any, headlines: Sequence[Dict[str, Any]], *, provider: str,
    lookback_hours: float, now: datetime,
) -> Dict[str, Any]:
    """The news read, weighed by how fresh, reliable and wide its evidence is."""
    items: List[Dict[str, Any]] = []
    usable: List[Dict[str, Any]] = []
    lookback = max(float(lookback_hours), FRESH_HOURS + 1.0)
    for head in headlines or []:
        age = _age_hours(head.get("published_at"), now)
        fresh = _freshness(age, lookback) if age is not None else 0.0
        row = {
            "source": str(head.get("source") or "?"), "title": str(head.get("title") or "")[:160],
            "published_at": str(head.get("published_at") or ""),
            "age_hours": None if age is None else round(age, 1),
            "used": fresh > 0.0,
            "skipped": "" if fresh > 0.0 else ("no_timestamp" if age is None else "stale"),
        }
        items.append(row)
        if fresh > 0.0:
            usable.append({**row, "freshness": fresh, "raw": head})

    result: Dict[str, Any] = {
        "status": "unavailable", "why": "", "direction": None, "strength": None,
        "quality": 0.0, "reliability": 0.0, "freshness": 0.0, "coverage": 0.0,
        "n_items": len(items), "n_used": len(usable), "items": items[:10],
        "reasons": [], "risks": [], "missing": [], "reader": fundamental.source,
        "provider": provider,
    }
    if fundamental.source == "skipped":
        result["why"] = "skipped"
        result["missing"] = [_reason("missing_skipped")]
        return result
    if not items:
        result["why"] = "no_provider" if provider in ("none", "", None) else "no_headlines"
        result["missing"] = [_reason("missing_" + result["why"])]
        return result
    if not usable:
        result["why"] = "stale"
        result["missing"] = [_reason("missing_stale", n=len(items), hours=f"{lookback:g}")]
        return result

    read = fundamental
    stale = len(items) - len(usable)
    if stale and read.source == "lexicon":
        # The cheap keyword reader can simply be re-run on the fresh items only.
        read = fundamental_module._lexicon_read([row["raw"] for row in usable])
    reader = "claude" if str(read.source).startswith("claude") else "lexicon"
    reliability = RELIABILITY.get(reader, 0.5)
    freshness = sum(row["freshness"] for row in usable) / len(usable)
    coverage = min(1.0, len(usable) / FULL_COVERAGE_ITEMS)
    result.update(
        status="available", why="", direction=_name(read.bias),
        strength=round(read.confidence, 1), reliability=reliability,
        freshness=round(freshness, 2), coverage=round(coverage, 2),
        quality=round(reliability * freshness * coverage, 3), reader=reader,
    )
    if read.summary:
        result["reasons"].append(_reason("text", text=read.summary))
    result["reasons"] += [_reason("text", text=text) for text in read.drivers[:3]]
    result["risks"] = [_reason("text", text=text) for text in read.risks[:3]]
    if reader == "lexicon":
        result["risks"].append(_reason("lexicon_note"))
    if stale and reader != "lexicon":
        result["risks"].append(_reason("mixed_age", n=stale))
    if stale:
        result["missing"].append(_reason("stale_dropped", n=stale))
    if coverage < 1.0:
        result["missing"].append(_reason("thin_coverage", n=len(usable), full=FULL_COVERAGE_ITEMS))
    return result


# -------------------------------------------------------------------- overall

def _sign(direction: Optional[str]) -> int:
    return 1 if direction == "BULLISH" else -1 if direction == "BEARISH" else 0


def _direction_of(score: float) -> str:
    if score >= DIRECTION_THRESHOLD:
        return "BULLISH"
    if score <= -DIRECTION_THRESHOLD:
        return "BEARISH"
    return "NEUTRAL"


def overall_result(tech: Dict[str, Any], fund: Dict[str, Any]) -> Dict[str, Any]:
    """Combine the two results by direction, quality and coverage -- not a plain average."""
    t_sign, t_strength = _sign(tech["direction"]), float(tech["strength"])
    if fund["status"] != "available":
        return {
            "direction": tech["direction"], "strength": round(t_strength, 1),
            "basis": "technical_only", "agreement": "none", "stronger": "technical",
            "reliability": "technical_only", "weights": {"technical": 1.0, "fundamental": 0.0},
            "direction_score": tech["score"], "fundamental_why": fund["why"],
            "steps": [_reason("combine_tech_only", why=fund["why"])],
        }

    f_sign, f_strength, quality = _sign(fund["direction"]), float(fund["strength"]), fund["quality"]
    w_t, w_f = TECH_WEIGHT, FUND_WEIGHT * quality
    total = w_t + w_f
    w_t, w_f = w_t / total, w_f / total
    d_t = t_sign * t_strength
    d_f = f_sign * f_strength
    direction_score = w_t * tech["score"] + w_f * f_sign * f_strength
    steps: List[Dict[str, Any]] = [_reason("weights", wt=f"{w_t * 100:.0f}", wf=f"{w_f * 100:.0f}",
                                           q=f"{quality * 100:.0f}")]
    if t_sign and f_sign and t_sign == f_sign:
        agreement, strength = "agree", w_t * t_strength + w_f * f_strength
        steps.append(_reason("combine_agree"))
    elif t_sign and f_sign:
        agreement, strength = "contradict", abs(w_t * d_t + w_f * d_f)
        steps.append(_reason("combine_contradict"))
    elif t_sign:
        agreement = "partial"
        strength = t_strength * (1.0 - UNCONFIRMED_DISCOUNT * quality)
        steps.append(_reason("combine_news_neutral"))
    elif f_sign:
        agreement, strength = "partial", w_f * f_strength
        steps.append(_reason("combine_tech_neutral"))
    else:
        agreement, strength = "partial", min(t_strength, f_strength)
        steps.append(_reason("combine_both_neutral"))
    effective = f_strength * quality
    gap = t_strength - effective
    stronger = "balanced" if abs(gap) < BALANCED_GAP else "technical" if gap > 0 else "fundamental"
    if agreement == "contradict":
        reliability = "reduced"
    elif agreement == "agree" and quality >= 0.5:
        reliability = "high"
    else:
        reliability = "moderate"
    return {
        "direction": _direction_of(direction_score), "strength": round(min(100.0, strength), 1),
        "basis": "combined", "agreement": agreement, "stronger": stronger,
        "reliability": reliability, "weights": {"technical": round(w_t, 3), "fundamental": round(w_f, 3)},
        "direction_score": round(direction_score, 1), "fundamental_why": "", "steps": steps,
    }


def compute(
    technical: Any, fundamental: Any, headlines: Sequence[Dict[str, Any]], *,
    legacy_strength: float, legacy_bias: Bias, provider: str, lookback_hours: float,
    now: datetime,
) -> Dict[str, Any]:
    """All three results, plus the old numbers for comparison. Pure; changes nothing."""
    tech = technical_result(technical)
    fund = fundamental_result(fundamental, headlines, provider=provider,
                              lookback_hours=lookback_hours, now=now)
    return {
        "version": VERSION, "technical": tech, "fundamental": fund,
        "overall": overall_result(tech, fund),
        "legacy": {"strength": round(float(legacy_strength), 1), "bias": _name(legacy_bias)},
        "params": PARAMS,
    }
