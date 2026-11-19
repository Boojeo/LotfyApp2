"""/why [epic] [days]: what has been stopping opportunities, from your own history."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..analysis import funnel
from ..models import utcnow

DEFAULT_DAYS = 7
TOP = 6


class WhyService:
    def __init__(self, supervisor: Any):
        self.sup = supervisor

    @property
    def t(self):
        return self.sup.t

    def _label(self, key: str) -> str:
        base, _, extra = key.partition(":")
        text = self.t(f"why.gate.{base}")
        return f"{text} ({extra})" if extra else text

    def command(self, argument: str) -> str:
        t = self.t
        days, epic = DEFAULT_DAYS, None
        for token in argument.split():
            if token.isdigit():
                days = max(1, min(int(token), 365))
            else:
                epic = self.sup.config.canonical_epic(token)
        plans = self.sup.store.plans_since(utcnow() - timedelta(days=days), epic)
        if not plans:
            return t("why.none", days=days)
        minimum = self.sup.config.auto.min_signal_strength
        s = funnel.summarise(plans, min_strength=minimum, t=t)
        n = s["n"]
        pct = lambda k: f"{k / n * 100:.0f}%"  # noqa: E731
        v = s["verdicts"]
        lines = [t("why.heading", days=days, n=n, epics=len(s["epics"])),
                 t("why.verdicts", a=v.get("APPROVED", 0), ap=pct(v.get("APPROVED", 0)),
                   c=v.get("CAUTION", 0), cp=pct(v.get("CAUTION", 0)),
                   r=v.get("REJECTED", 0), rp=pct(v.get("REJECTED", 0))),
                 t("why.neutral", n=s["neutral"], p=pct(s["neutral"]))]
        lines.append(t("why.auto_head", minimum=f"{minimum:g}"))
        for rank, (key, count) in enumerate(s["auto"].most_common(TOP), start=1):
            label = t("why.gate.pass") if key == funnel.PASS else self._label(key)
            lines.append(t("why.row", rank=rank, reason=label, n=count, p=pct(count)))
        lines.append(t("why.manual_head"))
        for rank, (key, count) in enumerate(s["manual"].most_common(TOP), start=1):
            reason = t(key) if key.startswith("verdict.") else key
            lines.append(t("why.row", rank=rank, reason=reason, n=count, p=pct(count)))
        if s["flags"]:
            flags = " · ".join(f"{k} {pct(c)}" for k, c in s["flags"].most_common(TOP))
            lines.append(t("why.flags", flags=flags))
        if s["directional"]:
            lines.append(t("why.strength", median=f"{s['strength_median']:.0f}",
                           top=f"{s['strength_max']:.0f}", minimum=f"{minimum:g}",
                           ok=s["strength_ok"], d=s["directional"]))
        shadowed = [p for p in plans if p.shadow]
        if shadowed:        # shadow scoring only: comparison, never used for a decision
            old = sum(1 for p in shadowed if not p.advisory_only and p.bias.direction is not None
                      and p.confidence >= minimum)
            new = sum(1 for p in shadowed
                      if p.shadow["overall"]["direction"] != "NEUTRAL"
                      and p.shadow["overall"]["strength"] >= minimum)
            only = sum(1 for p in shadowed if p.shadow["overall"]["basis"] == "technical_only")
            lines.append(t("why.shadow", n=len(shadowed), minimum=f"{minimum:g}", old=old, new=new,
                           only=only))
        if len(s["epics"]) > 1 or epic is None:
            lines.append(t("why.per_epic"))
            for name, row in sorted(s["epics"].items()):
                top_key, top_n = row["blockers"].most_common(1)[0]
                top = t("why.gate.pass") if top_key == funnel.PASS else self._label(top_key)
                lines.append(t("why.epic_row", epic=name, n=row["n"], a=row["approved"],
                               top=top, tn=top_n))
        lines.append(t("why.note"))
        return "\n".join(lines)
