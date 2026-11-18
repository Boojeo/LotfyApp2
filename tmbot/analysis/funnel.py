"""Why were opportunities not taken?  Counts, from the plans the bot already stores.

Every plan the bot builds is saved with its verdict, quality flags, signal
strength and risk check.  This replays the DECISION on those stored plans and
counts what stopped each one, so the answer comes from your own history rather
than from reading the code and guessing.

Two views, because the two modes decide differently:

* ``auto_blocker`` -- the first check that would stop automatic mode, in the
  exact order ``manage.auto.AutoTrader._judge`` runs its plan-level checks (the
  time-dependent ones -- cool-down, current price -- are not part of a stored
  plan and are left out).  A test keeps the two in step.
* ``manual_blocker`` -- the reason the report's verdict gave.

It counts decisions.  It does NOT say whether a decision was right; that needs
what happened afterwards, which ``analysis.replay`` measures on past candles.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any, Dict, List, Sequence

from ..i18n import Translator
from ..models import TradePlan
from . import report as report_module

PASS = "pass"


def auto_blocker(plan: TradePlan, *, min_strength: float, t: Translator) -> str:
    """The first plan-level check that stops automatic mode, or ``pass``."""
    found = plan.assessment or {}
    if plan.advisory_only or plan.bias.direction is None:
        return "no_direction"
    if plan.bias.direction is not plan.direction:
        return "against_bias"
    if found.get("verdict") != "APPROVED":
        return f"not_approved:{found.get('verdict', '-')}"
    if plan.confidence < min_strength:
        return "weak_signal"
    if found.get("alignment") != "ALIGNED":
        return "not_aligned"
    sizing = plan.sizing or {}
    if not sizing or sizing.get("verdict") == "UNCHECKED":
        return "risk_unchecked"
    if sizing.get("verdict") not in ("OK", "REDUCED_LEGS") or not sizing.get("legs"):
        return "risk_rejected"
    if report_module.detail_reasons(plan, t):
        return "doubtful"
    zone = plan.entry_zone
    if plan.entry_zone is not None and zone and not zone.get("valid"):
        return "entry_range"
    return PASS


def manual_blocker(plan: TradePlan) -> str:
    """What the report's verdict cited (its own translation key)."""
    found = plan.assessment or {}
    return str(found.get("verdict_key") or ("verdict.no_direction" if plan.advisory_only else "-"))


def summarise(
    plans: Sequence[TradePlan], *, min_strength: float, t: Translator
) -> Dict[str, Any]:
    """Counts over ``plans``: verdicts, blockers for both modes, flags, strength."""
    out: Dict[str, Any] = {"n": len(plans)}
    if not plans:
        return out
    directional = [p for p in plans if not p.advisory_only and p.bias.direction is not None]
    out["verdicts"] = Counter((p.assessment or {}).get("verdict", "-") for p in plans)
    out["neutral"] = len(plans) - len(directional)
    out["auto"] = Counter(auto_blocker(p, min_strength=min_strength, t=t) for p in plans)
    out["manual"] = Counter(manual_blocker(p) for p in plans)
    out["flags"] = Counter(
        flag["key"] for p in plans for flag in (p.assessment or {}).get("flags", []))
    strengths = [p.confidence for p in directional]
    out["strength_median"] = statistics.median(strengths) if strengths else 0.0
    out["strength_max"] = max(strengths) if strengths else 0.0
    out["strength_ok"] = sum(1 for s in strengths if s >= min_strength)
    out["directional"] = len(directional)
    per_epic: Dict[str, Dict[str, Any]] = {}
    for plan in plans:
        row = per_epic.setdefault(plan.epic, {"n": 0, "approved": 0, "blockers": Counter()})
        row["n"] += 1
        row["approved"] += int((plan.assessment or {}).get("verdict") == "APPROVED")
        blocker = auto_blocker(plan, min_strength=min_strength, t=t)
        row["blockers"][blocker] += 1
    out["epics"] = per_epic
    return out
