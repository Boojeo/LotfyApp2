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


THEMES: Dict[str, Theme] = {
    # Validated against the chart surface: target/stop clear the CVD floor, and
    # every colour clears 3:1 contrast.
    "light": Theme(
        surface="#fcfcfb", ink="#0b0b0b", secondary="#52514e", muted="#898781",
        grid="#e1e0d9", target="#1baf7a", stop="#d03b3b", zone="#2a78d6",
        entry="#52514e", ema_fast="#898781", ema_slow="#c3c2b7",
    ),
    "dark": Theme(
        surface="#1a1a19", ink="#ffffff", secondary="#c3c2b7", muted="#898781",
        grid="#2c2c2a", target="#2fc98c", stop="#e66767", zone="#5598e7",
        entry="#c3c2b7", ema_fast="#898781", ema_slow="#52514e",
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


def render(
    plan: TradePlan,
    candles: Sequence[Candle],
    output_path: str | Path,
    *,
    t: Optional[Translator] = None,
    theme: str = "light",
    bars: int = 120,
    entry_price: Optional[float] = None,
) -> Path:
    """Draw ``plan`` over ``candles`` and write a PNG.  Returns the path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    t = t or Translator()
    palette = THEMES.get(theme, THEMES["light"])
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
    # Colourless on purpose: the only colour on this chart is a decision.
    for index, candle in enumerate(window):
        rising = candle.close >= candle.open
        axes.vlines(index, candle.low, candle.high,
                    color=palette.secondary, linewidth=0.8, zorder=2)
        height = abs(candle.close - candle.open) or (plan.atr * 0.01)
        axes.add_patch(Rectangle(
            (index - 0.3, min(candle.open, candle.close)), 0.6, height,
            facecolor=palette.surface if rising else palette.secondary,
            edgecolor=palette.secondary, linewidth=0.8, zorder=3,
        ))

    # ---------------------------------------------------------------- moving averages
    for period, colour, name in (
        (20, palette.ema_fast, "EMA20"), (50, palette.ema_slow, "EMA50")
    ):
        series = ema(closes, period)
        points = [(i, v) for i, v in enumerate(series) if v is not None]
        if points:
            axes.plot([p[0] for p in points], [p[1] for p in points],
                      color=colour, linewidth=1.4, zorder=4, label=name)

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
            color=colour, fontsize=9, fontweight="bold", zorder=6,
        )

    axes.plot([0, right], [reference, reference], color=palette.entry,
              linewidth=1.2, linestyle=":", zorder=5)
    axes.annotate(
        label(f"{t('chart.entry')}  {reference:g}"),
        xy=(right + 1.2, reference), va="center", ha="left",
        color=palette.entry, fontsize=9, zorder=6,
    )

    # ---------------------------------------------------------------- chrome
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
        label(t("chart.title", epic=plan.epic, bias=t.bias_name(plan.bias),
                confidence=f"{plan.confidence:.0f}")),
        color=palette.ink, fontsize=13, fontweight="bold", loc="left", pad=14,
    )

    legend = axes.legend(
        loc="upper left", frameon=False, fontsize=8, labelcolor=palette.muted
    )
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
