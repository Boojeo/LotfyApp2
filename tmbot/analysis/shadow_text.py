"""Telegram text for the three-part analysis (shadow only).

Shown only when the plan carries a shadow result, i.e. ``scoring.mode: shadow``.
Pure formatting of what ``analysis.scoring`` already computed.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..i18n import Translator

_EMOJI = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}
FACTORS = ("trend_structure", "ema_cross", "ema_slope", "macd", "rsi", "adx_direction",
           "range_position", "momentum")


def reason_text(r: Dict[str, Any], t: Translator) -> str:
    key, args = r.get("key", ""), dict(r.get("args") or {})
    if key == "text":
        return str(args.get("text", ""))
    if "factor" in args:
        name = str(args["factor"])
        args["factor"] = t(f"shadow.factor.{name}") if f"shadow.factor.{name}" in _catalog() else name
    return t(f"shadow.r.{key}", **args)


def _catalog():
    from ..i18n import CATALOG
    return CATALOG


def _bias(t: Translator, direction: Any) -> str:
    return f"{_EMOJI.get(str(direction), '⚪')} {t.bias_name(direction, arrow=False)}"


def _why_fund(fund: Dict[str, Any], t: Translator) -> str:
    return t(f"shadow.why.{fund.get('why') or 'no_headlines'}")


def short_lines(shadow: Dict[str, Any], t: Translator) -> List[str]:
    """One line per analysis, each with its main reason, for the short message."""
    if not shadow:
        return []
    tech, fund, over = shadow["technical"], shadow["fundamental"], shadow["overall"]
    names = [t(f"shadow.factor.{r['args']['factor']}") if "factor" in r["args"] and
             f"shadow.factor.{r['args']['factor']}" in _catalog() else reason_text(r, t)
             for r in tech["supports"][:2]]
    why = t.semicolon.join(names) if names else "-"
    lines = [
        t("shadow.head"),
        t("shadow.tech_line", bias=_bias(t, tech["direction"]), strength=f"{tech['strength']:.0f}",
          score=f"{tech['score']:+.0f}", why=why),
    ]
    if fund["status"] == "available":
        lead = (fund["reasons"] or [None])[0]
        lines.append(t("shadow.fund_line", bias=_bias(t, fund["direction"]),
                       strength=f"{fund['strength']:.0f}", score=f"{_fscore(fund):+.0f}",
                       quality=f"{fund['quality'] * 100:.0f}",
                       why=fund.get("lead") or (reason_text(lead, t)[:90] if lead else "-")))
    else:
        lines.append(t("shadow.fund_none", why=_why_fund(fund, t)))
    lines.append(t("shadow.overall_line", bias=_bias(t, over["direction"]),
                   strength=f"{over['strength']:.0f}",
                   score=f"{over['direction_score']:+.0f}", how=_how(over, t)))
    return lines


def _fscore(fund: Dict[str, Any]) -> float:
    """Signed fundamental score: the source's own, or direction x strength."""
    if fund.get("score") is not None and "asset" in fund:
        return float(fund["score"])
    sign = {"BULLISH": 1, "BEARISH": -1}.get(str(fund.get("direction")), 0)
    return sign * float(fund.get("strength") or 0.0)


def _how(over: Dict[str, Any], t: Translator) -> str:
    if over["basis"] == "technical_only":
        return t("shadow.how_tech_only")
    return t("shadow.how_combined", agreement=t(f"shadow.agree.{over['agreement']}"),
             stronger=t(f"shadow.stronger.{over['stronger']}"),
             reliability=t(f"shadow.reliab.{over['reliability']}"))


def full_lines(shadow: Dict[str, Any], t: Translator) -> List[str]:
    """The three analyses with their reasons, weaknesses, data quality and how they combine."""
    if not shadow:
        return []
    tech, fund, over = shadow["technical"], shadow["fundamental"], shadow["overall"]
    legacy = shadow.get("legacy") or {}
    lines = [t("shadow.full_head"), t("shadow.full_note", old=f"{legacy.get('strength', 0):.0f}"), ""]

    lines.append(t("shadow.tech_head", bias=_bias(t, tech["direction"]), strength=f"{tech['strength']:.0f}",
                    score=f"{tech['score']:+.0f}"))
    lines += ["• " + t("shadow.label.because") + " " + reason_text(r, t) for r in tech["supports"]]
    lines += ["• " + t("shadow.label.against") + " " + reason_text(r, t) for r in tech["conflicts"]]
    lines += ["• " + t("shadow.label.weak") + " " + reason_text(r, t) for r in tech["weaknesses"]]
    lines.append("")

    if fund["status"] == "available":
        lines.append(t("shadow.fund_head", bias=_bias(t, fund["direction"]),
                       strength=f"{fund['strength']:.0f}", score=f"{_fscore(fund):+.0f}"))
        lines.append("• " + t("shadow.label.quality", q=f"{fund['quality'] * 100:.0f}",
                              rel=f"{fund['reliability'] * 100:.0f}", fresh=f"{fund['freshness'] * 100:.0f}",
                              cov=f"{fund['coverage'] * 100:.0f}", used=fund["n_used"], total=fund["n_items"],
                              reader=fund["reader"]))
        lines += ["• " + t("shadow.label.because") + " " + reason_text(r, t) for r in fund["reasons"]]
        lines += ["• " + t("shadow.label.weak") + " " + reason_text(r, t) for r in fund["risks"]]
    else:
        lines.append(t("shadow.fund_head_none", why=_why_fund(fund, t)))
    lines += _item_lines(fund, t)
    if fund.get("event_risk"):
        lines.append("• ⏰ " + t("shadow.label.event") + " " + "; ".join(fund["event_risk"]))
    lines += ["• " + t("shadow.label.missing") + " " + reason_text(r, t) for r in fund["missing"]]
    lines.append("")

    lines.append(t("shadow.overall_head", bias=_bias(t, over["direction"]), strength=f"{over['strength']:.0f}",
                   score=f"{over['direction_score']:+.0f}"))
    lines.append("• " + _how(over, t))
    lines += ["• " + reason_text(r, t) for r in over["steps"]]
    if over["basis"] == "combined":
        lines.append("• " + t("shadow.label.reliab", text=t(f"shadow.reliab.{over['reliability']}")))
    return lines


def _item_lines(fund: Dict[str, Any], t: Translator) -> List[str]:
    """Every source used: its tier, name, age, what it said, and whether it counted."""
    out = []
    for item in fund.get("items", [])[:9]:
        age = item.get("age_hours")
        when = t("shadow.age.future") if age is None and item.get("published_at") and not item.get("skipped") \
            else t("shadow.age", h=age) if age is not None else "?"
        state = ("shadow.item.scored" if item.get("used") else
                 "shadow.item.stale" if item.get("skipped") else "shadow.item.context")
        out.append("  – " + t("shadow.label.item", tier=t(f"shadow.tier.{item.get('tier') or 'headline'}"),
                              source=item["source"], age=when, title=item["title"][:110])
                   + f" [{t(state)}]")
    return out
