"""Annotated plan charts.

The picture answers one question: *where are my levels relative to price, and
what does each one pay?*  So the candles are deliberately colourless -- drawn
in plain ink -- and the only colour on the chart belongs to decisions: targets,
the stop, and the risk you are carrying.

Colour choice is not taste here.  The conventional take-profit green
(``#0ca30c``) and stop red (``#d03b3b``) sit 4.1 apart in deuteranopia, which
means roughly one man in twelve could not tell a target line from a stop line.
The teal used instead scores 9.9 on the same measure.  Every level is also
directly labelled and drawn in its own line style, so the chart never relies on
colour alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..i18n import Translator
from ..models import Candle, TradePlan
from .indicators import ema, last_value

log = logging.getLogger(__name__)

# Fonts that actually carry Arabic presentation forms, best first.  Tahoma and
# Arial are always present on Windows; DejaVu ships with matplotlib.
ARABIC_FONTS = ("Noto Sans Arabic", "Amiri", "Tahoma", "Arial", "DejaVu Sans")


@dataclass(frozen=True)
class Theme:
    surface: str
    ink: str
    secondary: str
    muted: str
    grid: str
    target: str
    stop: str
    zone: str
    entry: str
    ema_fast: str
    ema_slow: str
    up_fill: str        # rising candle body
    up_edge: str
    down_fill: str      # falling candle body
    down_edge: str
    fib: str            # analysis tools drawn on the chart
    gap: str
    camarilla: str
    wolfe: str
    static: str
    tag: str            # background of the entry / last-price tag


THEMES: Dict[str, Theme] = {
    # Validated against the chart surface: target/stop clear the CVD floor, and
    # every colour clears 3:1 contrast.
    "light": Theme(
        surface="#fcfcfb", ink="#0b0b0b", secondary="#52514e", muted="#898781",
        grid="#e1e0d9", target="#1baf7a", stop="#d03b3b", zone="#2a78d6",
        entry="#52514e", ema_fast="#898781", ema_slow="#c3c2b7",
        up_fill="#fcfcfb", up_edge="#52514e", down_fill="#52514e", down_edge="#52514e",
        fib="#b8860b", gap="#8e44ad", camarilla="#7f8c8d", wolfe="#c0392b",
        static="#2a78d6", tag="#52514e",
    ),
    "dark": Theme(
        surface="#1a1a19", ink="#ffffff", secondary="#c3c2b7", muted="#898781",
        grid="#2c2c2a", target="#2fc98c", stop="#e66767", zone="#5598e7",
        entry="#c3c2b7", ema_fast="#898781", ema_slow="#52514e",
        up_fill="#1a1a19", up_edge="#c3c2b7", down_fill="#c3c2b7", down_edge="#c3c2b7",
        fib="#f2b84b", gap="#b388ff", camarilla="#9598a1", wolfe="#ff7597",
        static="#5598e7", tag="#52514e",
    ),
    # TradingView's dark look: navy surface, filled candles, price tags.
    "tv": Theme(
        surface="#131722", ink="#d1d4dc", secondary="#787b86", muted="#787b86",
        grid="#2a2e39", target="#26a69a", stop="#ef5350", zone="#2962ff",
        entry="#787b86", ema_fast="#f5f5f5", ema_slow="#ff9800",
        up_fill="#2196f3", up_edge="#2196f3", down_fill="#ef5350", down_edge="#ef5350",
        fib="#f2b84b", gap="#b388ff", camarilla="#9598a1", wolfe="#ff7597",
        static="#5b8def", tag="#434651",
    ),
}


# Directional isolates and marks.  They are exactly right for Telegram and a
# terminal, which run a modern bidi implementation; they are wrong here.
_BIDI_CONTROLS = str.maketrans("", "", "\u2066\u2067\u2068\u2069\u200e\u200f")


def shape(text: str, rtl: bool) -> str:
    """Reshape and reorder Arabic so matplotlib draws it correctly.

    Two different renderers need two different treatments.  Telegram and the
    terminal lay out bidi themselves, so there we *insert* isolation marks
    around numbers.  Matplotlib has no bidi engine and does not join Arabic
    letters, so here we *strip* those marks and do the shaping and reordering
    ourselves -- ``python-bidi`` implements the pre-isolate algorithm and
    raises on a U+2068 it never expected to see.

    If the libraries are missing the text comes back unchanged: an unshaped
    label beats no chart.
    """
    if not rtl or not text:
        return text
    text = text.translate(_BIDI_CONTROLS)
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError:
        log.warning("arabic-reshaper/python-bidi missing; Arabic labels will not join")
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception as exc:  # never lose a chart to a text-shaping edge case
        log.warning("Arabic shaping failed (%s); drawing the raw string", exc)
        return text


def _configure_font(rtl: bool) -> None:
    from matplotlib import font_manager, rcParams
    if not rtl:
        return
    available = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in ARABIC_FONTS:
        if candidate in available:
            rcParams["font.family"] = [candidate]
            return
    log.warning("no Arabic-capable font found; chart labels may render as boxes")



def _draw_tools(axes, plan, window, offset, palette, label, bounds, t):
    """Draw the market-map tools that fall on this chart; return what was drawn.

    Only tools that actually appear are returned, so the legend lists exactly
    what the reader can see.  ``bounds`` keeps off-screen lines from stretching
    the price axis.
    """
    from matplotlib.patches import Rectangle
    market = plan.market or {}
    low, high = bounds
    right = len(window) - 1
    inside = lambda price: low <= price <= high  # noqa: E731
    used: List[tuple] = []

    fib = market.get("fibonacci")
    if fib:
        retr = {lv["ratio"]: lv["price"] for lv in fib["levels"] if lv["kind"] == "retracement"}
        if 0.618 in retr and 0.786 in retr and inside(retr[0.618]) and inside(retr[0.786]):
            axes.axhspan(min(retr[0.618], retr[0.786]), max(retr[0.618], retr[0.786]),
                         color=palette.fib, alpha=0.14, linewidth=0, zorder=0)
        drawn = False
        for ratio, price in retr.items():
            if inside(price):
                drawn = True
                axes.plot([0, right], [price, price], color=palette.fib, linewidth=0.9,
                          linestyle=":", zorder=1)
                axes.annotate(f"{ratio * 100:.1f}%", xy=(0.5, price), va="bottom",
                              ha="left", color=palette.fib, fontsize=7, zorder=6)
        if drawn:
            used.append((t("chart.tool.fib"), palette.fib, ":"))

    for gap in (market.get("gaps") or [])[:1]:
        start = right - gap["bars_ago"] - 2
        if start < 0 or not (inside(gap["low"]) and inside(gap["high"])):
            continue
        axes.add_patch(Rectangle((start, gap["low"]), right - start, gap["high"] - gap["low"],
                                 facecolor=palette.gap, alpha=0.22, linewidth=0, zorder=1))
        axes.annotate(label(f"FVG {t.term('gaptype', gap['type'])}"),
                      xy=(start + 0.5, gap["high"]), va="bottom", ha="left",
                      color=palette.gap, fontsize=7.5, fontweight="bold", zorder=6)
        used.append((t("chart.tool.gap"), palette.gap, "-"))

    cam = market.get("camarilla")
    if cam:
        drawn = False
        for key in ("r4", "r3", "s3", "s4"):
            if inside(cam[key]):
                drawn = True
                axes.plot([0, right], [cam[key], cam[key]], color=palette.camarilla,
                          linewidth=0.9, linestyle="-.", zorder=1)
                axes.annotate(key.upper(), xy=(right - 0.5, cam[key]), va="bottom",
                              ha="right", color=palette.camarilla, fontsize=7, zorder=6)
        if drawn:
            used.append((t("chart.tool.camarilla"), palette.camarilla, "-."))

    wolfe = market.get("wolfe")
    if wolfe:
        points = [(index - offset, price) for index, price in wolfe["points"]]
        if all(0 <= x <= right and inside(p) for x, p in points):
            for first, second in ((0, 2), (1, 3), (0, 3)):
                axes.plot([points[first][0], points[second][0]],
                          [points[first][1], points[second][1]],
                          color=palette.wolfe, linewidth=1.0, linestyle="--", zorder=4)
            for number, (x, price) in enumerate(points, start=1):
                axes.annotate(str(number), xy=(x, price), xytext=(0, -11 if number % 2 else 7),
                              textcoords="offset points", ha="center", color=palette.wolfe,
                              fontsize=8, fontweight="bold", zorder=6)
            if inside(wolfe["target"]):
                axes.plot([right], [wolfe["target"]], marker="*", color=palette.wolfe,
                          markersize=9, zorder=6)
            used.append((t("chart.tool.wolfe"), palette.wolfe, "--"))

    static = market.get("static") or {}
    if static.get("support") or static.get("resistance"):
        used.append((t("chart.tool.static"), palette.static, "-"))
    return used


def render(
    plan: TradePlan,
    candles: Sequence[Candle],
    output_path: str | Path,
    *,
    t: Optional[Translator] = None,
    theme: str = "tv",
    bars: int = 120,
    entry_price: Optional[float] = None,
) -> Path:
    """Draw ``plan`` over ``candles`` and write a PNG.  Returns the path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    t = t or Translator()
    palette = THEMES.get(theme, THEMES["tv"])
    _configure_font(t.is_rtl)

    def label(text: str) -> str:
        return shape(text, t.is_rtl)

    window = list(candles)[-bars:]
    if len(window) < 5:
        raise ValueError(f"need at least 5 candles to draw a chart, got {len(window)}")

    closes = [c.close for c in window]
    reference = entry_price if entry_price is not None else plan.reference_price
    levels = [
        (plan.tp3, "TP3", palette.target, "-"),
        (plan.tp2, "TP2", palette.target, "-"),
        (plan.tp1, "TP1", palette.target, "-"),
        (plan.sl, "SL", palette.stop, "--"),
    ]

    figure, axes = plt.subplots(figsize=(11, 6.2), dpi=150)
    figure.patch.set_facecolor(palette.surface)
    axes.set_facecolor(palette.surface)

    # ---------------------------------------------------------------- zones
    half = max(plan.atr * 0.22, 1e-9)
    for zone in sorted(plan.levels, key=lambda z: z.score, reverse=True)[:6]:
        axes.axhspan(
            zone.price - half, zone.price + half,
            color=palette.zone, alpha=0.16 if zone.is_liquidity else 0.07,
            linewidth=0, zorder=0,
        )

    # ---------------------------------------------------------------- risk band
    axes.axhspan(min(reference, plan.sl), max(reference, plan.sl),
                 color=palette.stop, alpha=0.07, linewidth=0, zorder=0)

    # ---------------------------------------------------------------- candles
    for index, candle in enumerate(window):
        rising = candle.close >= candle.open
        edge = palette.up_edge if rising else palette.down_edge
        axes.vlines(index, candle.low, candle.high, color=edge, linewidth=0.9, zorder=2)
        height = abs(candle.close - candle.open) or (plan.atr * 0.01)
        axes.add_patch(Rectangle(
            (index - 0.33, min(candle.open, candle.close)), 0.66, height,
            facecolor=palette.up_fill if rising else palette.down_fill,
            edgecolor=edge, linewidth=0.8, zorder=3,
        ))

    # ---------------------------------------------------------------- moving averages
    for period, colour, name in (
        (20, palette.ema_fast, "EMA20"), (50, palette.ema_slow, "EMA50")
    ):
        series = ema(closes, period)
        points = [(i, v) for i, v in enumerate(series) if v is not None]
        if points:
            axes.plot([p[0] for p in points], [p[1] for p in points],
                      color=colour, linewidth=1.4, zorder=4)

    # ---------------------------------------------------------------- levels
    # Lines stop at the last candle so the label sits in clear space beside
    # them -- drawn full width, a level strikes through its own text.
    right = len(window) - 1
    gutter = max(6, int(len(window) * 0.30))
    for price, name, colour, style in levels:
        axes.plot([0, right], [price, price], color=colour, linewidth=1.8,
                  linestyle=style, zorder=5, solid_capstyle="butt")
        reward = plan.reward_risk(price) if name != "SL" else -1.0
        axes.annotate(
            label(f"{name}  {price:g}  ({reward:+.2f}R)"),
            xy=(right + 1.2, price), va="center", ha="left",
            color="white", fontsize=8.5, fontweight="bold", zorder=6,
            bbox=dict(boxstyle="round,pad=0.25", facecolor=colour, edgecolor="none"),
        )

    axes.plot([0, right], [reference, reference], color=palette.entry,
              linewidth=1.2, linestyle=":", zorder=5)
    axes.annotate(
        label(f"{t('chart.entry')}  {reference:g}"),
        xy=(right + 1.2, reference), va="center", ha="left",
        color="white", fontsize=8.5, zorder=6,
        bbox=dict(boxstyle="round,pad=0.25", facecolor=palette.tag, edgecolor="none"),
    )
    last = window[-1]
    axes.annotate(
        f"{last.close:g}", xy=(right + 1.2, last.close), xytext=(0, -16),
        textcoords="offset points", va="center", ha="left", color="white",
        fontsize=8.5, fontweight="bold", zorder=7,
        bbox=dict(boxstyle="round,pad=0.25", edgecolor="none",
                  facecolor=palette.up_fill if last.close >= last.open else palette.down_fill),
    )

    # ---------------------------------------------------------------- analysis tools
    used = _draw_tools(axes, plan, window, len(candles) - len(window), palette, label,
                       (min(min(c.low for c in window), plan.sl, plan.tp1),
                        max(max(c.high for c in window), plan.tp3, plan.sl)), t)

    # ---------------------------------------------------------------- chrome
    axes.yaxis.tick_right()
    axes.set_xlim(-1, right + gutter)
    axes.grid(axis="y", color=palette.grid, linewidth=0.7, zorder=1)
    axes.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        axes.spines[spine].set_visible(False)
    axes.spines["bottom"].set_color(palette.grid)
    axes.tick_params(colors=palette.muted, labelsize=8)

    ticks = list(range(0, len(window), max(1, len(window) // 7)))
    # Intraday windows need the time, or every tick reads the same date.
    span_hours = (window[-1].ts - window[0].ts).total_seconds() / 3600
    stamp = "%d %b" if span_hours > 96 else "%d %b %H:%M"
    axes.set_xticks(ticks)
    axes.set_xticklabels([window[i].ts.strftime(stamp) for i in ticks])

    axes.set_title(
        label(t("chart.title", epic=plan.epic, bias=t.bias_name(plan.bias, arrow=False),
                confidence=f"{plan.confidence:.0f}")),
        color=palette.ink, fontsize=13, fontweight="bold", loc="left", pad=14,
    )

    from matplotlib.lines import Line2D
    handles = [Line2D([0], [0], color=colour, linestyle=style, linewidth=2,
                      label=label(name)) for name, colour, style in used]
    role = f" · {label(t('chart.tool.dynamic'))}" if (plan.market or {}).get("dynamic") else ""
    handles += [Line2D([0], [0], color=palette.ema_fast, linewidth=2, label=f"EMA20{role}"),
                Line2D([0], [0], color=palette.ema_slow, linewidth=2, label=f"EMA50{role}")]
    legend = axes.legend(handles=handles, title=label(t("chart.tools")), loc="upper left",
                         frameon=False, fontsize=8, title_fontsize=8,
                         labelcolor=palette.muted, alignment="left")
    legend.get_title().set_color(palette.ink)
    for text_item in legend.get_texts():
        text_item.set_color(palette.muted)

    technical = plan.technical
    segments = [
        t("chart.atr", value=f"{plan.atr:.4g}"),
        t("chart.adx", value=technical.get("adx", "-"),
          strength=t.strength_name(technical.get("strength", ""))),
        t("chart.rsi", value=technical.get("rsi", "-")),
        t("chart.risk", value=f"{plan.risk:.4g}"),
    ]
    # Shape each segment on its own, then lay them out in reading order.
    # Joining first and shaping once lets the algorithm reorder across the
    # separators, which pairs a value with the wrong label.
    shaped = [label(segment) for segment in segments]
    summary = "   ".join(reversed(shaped) if t.is_rtl else shaped)
    figure.text(
        0.988 if t.is_rtl else 0.012, 0.022, summary,
        color=palette.muted, fontsize=8.5, ha="right" if t.is_rtl else "left",
    )
    figure.text(
        0.012 if t.is_rtl else 0.988, 0.022, plan.plan_id,
        color=palette.muted, fontsize=7.5, ha="left" if t.is_rtl else "right",
    )

    figure.tight_layout(rect=(0, 0.045, 1, 1))
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, facecolor=palette.surface)
    plt.close(figure)
    log.info("wrote chart %s", path)
    return path
