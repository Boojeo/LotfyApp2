"""Old score vs the three-part shadow analysis, and the records export.

Everything here READS: replayed decisions (``analysis.replay``) or stored plans
opened read-only.  Nothing is written except the export file you name, and that
file is created new -- an existing file is never overwritten.  No order function
is reachable from here.

The comparison answers, from your own history:
  * how many setups reach each strength level under the old score, the
    technical analysis alone, and the overall (combined) analysis;
  * why setups failed the existing filters (first failure, and every failure);
  * a HYPOTHETICAL count: how many would pass if the strength check used the
    overall score and nothing else changed -- shown for comparison only;
  * when outcomes are known (a replay), how each strength level actually fared.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..i18n import Translator
from ..models import TradePlan
from . import funnel
from .replay import Record, group_stats

THRESHOLDS = (40, 50, 60, 70)
BUCKETS = (("<40", 0, 40), ("40-60", 40, 60), (">=60", 60, 101))


# ------------------------------------------------------------------ read-only sources

def read_plans(db_path: str, since: datetime, epic: Optional[str] = None) -> List[TradePlan]:
    """Stored plans, read through a read-only connection (cannot write or create)."""
    if not Path(db_path).is_file():
        return []
    connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        sql, params = "SELECT payload FROM plans WHERE created_at >= ?", [since.isoformat()]
        if epic:
            sql, params = sql + " AND epic = ?", params + [epic]
        out: List[TradePlan] = []
        for (payload,) in connection.execute(sql + " ORDER BY created_at", params):
            try:
                out.append(TradePlan.from_dict(json.loads(payload)))
            except (ValueError, KeyError):
                continue
        return out
    finally:
        connection.close()


def records_from_plans(plans: Sequence[TradePlan], *, min_strength: float, t: Translator) -> List[Record]:
    """Stored plans as records (no outcomes: those need candles after the plan)."""
    out = []
    for plan in plans:
        found = plan.assessment or {}
        out.append(Record(
            ts=plan.created_at, verdict=found.get("verdict", "-"), quality=found.get("quality", "-"),
            alignment=found.get("alignment", "-"), bias=plan.bias.value, strength=plan.confidence,
            flags=[f["key"] for f in found.get("flags", [])],
            blocker=funnel.auto_blocker(plan, min_strength=min_strength, t=t),
            sizing=(plan.sizing or {}).get("verdict", "-"),
            directional=not plan.advisory_only and plan.bias.direction is not None,
            gates=funnel.gate_results(plan, min_strength=min_strength, t=t),
            shadow=plan.shadow, epic=plan.epic,
        ))
    return out


# ------------------------------------------------------------------------ comparison

def _overall(record: Record) -> Dict[str, Any]:
    return (record.shadow or {}).get("overall") or {}


def _tech(record: Record) -> Dict[str, Any]:
    return (record.shadow or {}).get("technical") or {}


def _spread(values: Sequence[float]) -> Dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {"median": statistics.median(ordered), "p75": ordered[int(0.75 * (len(ordered) - 1))],
            "max": ordered[-1]}


def compare(records: Sequence[Record], *, min_strength: float) -> Dict[str, Any]:
    shadowed = [r for r in records if r.shadow]
    legacy_dir = [r for r in shadowed if r.directional]
    tech_dir = [r for r in shadowed if _tech(r).get("direction") not in (None, "NEUTRAL")]
    over_dir = [r for r in shadowed if _overall(r).get("direction") not in (None, "NEUTRAL")]
    out: Dict[str, Any] = {
        "records": len(records), "shadowed": len(shadowed), "min_strength": min_strength,
        "reach": {}, "distribution": {}, "basis": Counter(), "agreement": Counter(),
        "first_blocker": Counter(), "gate_failures": Counter(), "outcomes": {},
    }
    if not shadowed:
        return out
    for level in THRESHOLDS:
        out["reach"][level] = {
            "old": sum(1 for r in legacy_dir if r.strength >= level),
            "technical": sum(1 for r in tech_dir if _tech(r)["strength"] >= level),
            "overall": sum(1 for r in over_dir if _overall(r)["strength"] >= level),
        }
    out["distribution"] = {
        "old": _spread([r.strength for r in legacy_dir]),
        "technical": _spread([_tech(r)["strength"] for r in tech_dir]),
        "overall": _spread([_overall(r)["strength"] for r in over_dir]),
    }
    out["basis"] = Counter(_overall(r).get("basis", "-") for r in shadowed)
    out["agreement"] = Counter(_overall(r).get("agreement", "-") for r in shadowed)
    out["fundamental"] = Counter((r.shadow["fundamental"]["status"] +
                                  (":" + r.shadow["fundamental"]["why"] if r.shadow["fundamental"]["why"] else ""))
                                 for r in shadowed)
    out["first_blocker"] = Counter(r.blocker for r in shadowed)
    for r in shadowed:
        for gate in r.gates:
            if not gate["passed"]:
                out["gate_failures"][gate["gate"]] += 1
    others_ok = [r for r in shadowed if r.gates and all(
        g["passed"] for g in r.gates if g["gate"] != "strength")]
    out["accepted_old"] = sum(1 for r in shadowed if r.blocker == funnel.PASS)
    out["accepted_if_overall"] = sum(
        1 for r in others_ok
        if _overall(r).get("strength", 0) >= min_strength and _overall(r).get("direction") == r.bias)
    out["others_ok"] = len(others_ok)
    # Outcomes by strength bucket (only a replay has them).
    measured = [r for r in legacy_dir if r.outcome.get("r")]
    if measured:
        for name, key in (("old", lambda r: r.strength), ("overall", lambda r: _overall(r).get("strength", 0.0))):
            out["outcomes"][name] = {
                label: group_stats([r for r in measured if lo <= key(r) < hi]) for label, lo, hi in BUCKETS}
    return out


def render(result: Dict[str, Any], *, title: str = "") -> str:
    lines = [title or "OLD SCORE vs THREE-PART ANALYSIS (shadow)", ""]
    if not result["shadowed"]:
        lines.append("No decision has a shadow analysis: turn it on with scoring.mode: shadow "
                     "(or run the audit with --shadow / --records) and collect some first.")
        return "\n".join(lines)
    n = result["shadowed"]
    lines += [f"{n} decisions compared; the automatic-mode minimum is {result['min_strength']:g}.",
              "The old score is what every real decision used. The overall score is shadow: it "
              "decided nothing.", "",
              "How many directional setups reach each strength",
              f"  {'at least':<10s}{'old':>8s}{'technical':>12s}{'overall':>10s}"]
    for level, row in result["reach"].items():
        lines.append(f"  {level:<10d}{row['old']:>8d}{row['technical']:>12d}{row['overall']:>10d}")
    lines += ["", "Strength distribution (median / 75th percentile / max)"]
    for name, spread in result["distribution"].items():
        lines.append(f"  {name:<10s}" + ("  ".join(f"{k} {v:.0f}" for k, v in spread.items()) or "-"))
    lines += ["", "What the overall analysis was based on"]
    lines.append("  " + ", ".join(f"{k}: {v}" for k, v in result["basis"].most_common()))
    lines.append("  fundamental data: " + ", ".join(f"{k}: {v}" for k, v in result["fundamental"].most_common()))
    lines.append("  technical vs fundamental: " + ", ".join(f"{k}: {v}" for k, v in result["agreement"].most_common()))
    lines += ["", "Why setups failed the existing filters",
              "  first failing check: " + ", ".join(f"{k} {v}" for k, v in result["first_blocker"].most_common(8)),
              "  every failing check: " + ", ".join(f"{k} {v}" for k, v in result["gate_failures"].most_common())]
    lines += ["", "HYPOTHETICAL (comparison only, nothing was or will be traded on this)",
              f"  accepted by the existing rules: {result['accepted_old']}",
              f"  would pass if the strength check used the overall score: {result['accepted_if_overall']} "
              f"(of {result['others_ok']} that pass every other check)"]
    if result["outcomes"]:
        lines += ["", "How each strength level actually fared (replay; TP1 before the stop, mean R at TP1)"]
        for name, groups in result["outcomes"].items():
            for label, g in groups.items():
                if not g.get("n"):
                    lines.append(f"  {name:<8s}{label:<7s} n=0")
                    continue
                note = "" if g.get("enough") else "  (too few to judge)"
                lines.append(f"  {name:<8s}{label:<7s} n={g['n']:<5d} TP1 first {g['tp1_first'] * 100:5.1f}%  "
                             f"stop first {g['stop_first'] * 100:5.1f}%  R {g['mean_r'][0]:+.2f}{note}")
    lines += ["", "Samples overlap and exits ignore spread and trailing: past results, not a forecast."]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- export

COLUMNS = (
    "time", "epic", "old_bias", "old_strength", "old_verdict", "alignment", "flags", "first_blocker",
    "failed_checks", "passed_checks",
    "tech_direction", "tech_strength", "tech_score", "tech_adx",
    "fund_status", "fund_why", "fund_direction", "fund_strength", "fund_quality", "fund_items_used",
    "overall_direction", "overall_strength", "overall_basis", "agreement", "stronger", "overall_reliability",
    "tp1_first", "stop_first", "r_tp1", "r_tp2", "r_tp3",
)


def row_of(record: Record) -> Dict[str, Any]:
    tech, fund, over = _tech(record), (record.shadow or {}).get("fundamental") or {}, _overall(record)
    out = record.outcome or {}
    r = out.get("r") or [None, None, None]
    return {
        "time": record.ts.isoformat(), "epic": record.epic, "old_bias": record.bias,
        "old_strength": record.strength, "old_verdict": record.verdict, "alignment": record.alignment,
        "flags": "|".join(record.flags), "first_blocker": record.blocker,
        "failed_checks": "|".join(g["gate"] for g in record.gates if not g["passed"]),
        "passed_checks": "|".join(g["gate"] for g in record.gates if g["passed"]),
        "tech_direction": tech.get("direction"), "tech_strength": tech.get("strength"),
        "tech_score": tech.get("score"), "tech_adx": tech.get("adx"),
        "fund_status": fund.get("status"), "fund_why": fund.get("why"),
        "fund_direction": fund.get("direction"), "fund_strength": fund.get("strength"),
        "fund_quality": fund.get("quality"), "fund_items_used": fund.get("n_used"),
        "overall_direction": over.get("direction"), "overall_strength": over.get("strength"),
        "overall_basis": over.get("basis"), "agreement": over.get("agreement"),
        "stronger": over.get("stronger"), "overall_reliability": over.get("reliability"),
        "tp1_first": out.get("tp1_first"), "stop_first": out.get("stop_first"),
        "r_tp1": r[0], "r_tp2": r[1] if len(r) > 1 else None, "r_tp3": r[2] if len(r) > 2 else None,
    }


def write_records(path: str, records: Iterable[Record]) -> int:
    """Write CSV (or JSON lines for ``.jsonl``/``.json``) to a NEW file; never overwrite."""
    target = Path(path)
    rows = [row_of(r) for r in records]
    with open(target, "x", encoding="utf-8", newline="") as handle:     # "x": fail if it exists
        if target.suffix.lower() in (".jsonl", ".json"):
            for row in rows:
                handle.write(json.dumps(row, default=str) + "\n")
        else:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    return len(rows)
