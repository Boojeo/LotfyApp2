"""From raw official data to evidence: what each instrument cares about.

Every rule is a plain, printed formula -- a change in a published number mapped
to a signal between -1 (bearish for the instrument) and +1 (bullish), with a
weight.  The scale constants are provisional (the shadow data is what should
tune them) and every evidence line says what was measured.

What is NOT scored, on purpose, and shown as context instead: speculative
positioning (it can mean "crowded" or "trending"), inflation and jobs prints
(there is no free consensus forecast, so a "surprise" cannot be computed
honestly), weather (no free climate normals), central-bank statements and all
news.  Context never moves a score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import parsers

UTC = timezone.utc

CONFIRMED, SCHEDULED, BREAKING, UNVERIFIED = "confirmed", "scheduled", "breaking", "unverified"

DAILY_HOURS = 96.0          # a daily series is still current over a weekend
WEEKLY_HOURS = 240.0
POSITIONING_HOURS = 264.0
LOOKBACK = 5                # observations a "recent change" spans


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass
class Evidence:
    key: str
    label: str
    tier: str
    source: str
    published_at: Optional[datetime]       # when it was published / observed
    text: str                              # the measured value, in words
    signal: Optional[float] = None         # -1..+1 for the instrument; None = context only
    weight: float = 0.0                    # 0 = context only
    max_age_hours: float = DAILY_HOURS
    url: str = ""
    reason: str = ""                       # why that signal, in words

    @property
    def scored(self) -> bool:
        return self.signal is not None and self.weight > 0


# Instrument -> asset class, from the epic / display name.
ASSET_WORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("gold", ("XAU", "GOLD")),
    ("silver", ("XAG", "SILVER")),
    ("brent", ("UKOIL", "BRENT")),
    ("wti", ("USOIL", "WTI", "CRUDE")),
    ("natgas", ("XNG", "NATGAS", "NATURAL GAS", "NGAS")),
    ("eurusd", ("EURUSD", "EUR/USD")),
)

# What each asset is scored on (key -> weight): the denominator of "coverage",
# so a source that is down lowers the data quality instead of vanishing.
SCORED_SPECS: Dict[str, Dict[str, float]] = {
    "gold": {"real_yield": 3.0, "us2y": 1.5, "usd": 1.5},
    "silver": {"real_yield": 3.0, "us2y": 1.5, "usd": 1.5},
    "wti": {"crude": 3.0, "gasoline": 1.0, "distillate": 1.0, "usd": 1.0},
    "brent": {"crude": 3.0, "gasoline": 1.0, "distillate": 1.0, "usd": 1.0},
    "natgas": {"gas_storage": 3.0},
    "eurusd": {"spread2": 3.0, "spread10": 1.5},
}

# CFTC contract codes for the positioning context.
COT: Dict[str, Tuple[str, str]] = {
    "gold": ("disagg", "088691"), "silver": ("disagg", "084691"), "wti": ("disagg", "067651"),
    "brent": ("disagg", "06765T"), "natgas": ("disagg", "023651"), "eurusd": ("fin", "099741"),
}


def detect_asset(epic: str, display: str = "", override: Optional[str] = None) -> Optional[str]:
    if override:
        return override if override in SCORED_SPECS else None
    text = f"{epic} {display}".upper()
    for asset, words in ASSET_WORDS:
        if any(word in text for word in words):
            return asset
    return None


def _stamp(day_: date) -> datetime:
    return datetime(day_.year, day_.month, day_.day, tzinfo=UTC)


def _change(series: Sequence[Tuple[date, float]], lookback: int = LOOKBACK
            ) -> Optional[Tuple[float, float, date, float]]:
    """(latest, change over ``lookback`` observations, latest date, previous) or None."""
    if len(series) <= lookback:
        return None
    latest, earlier = series[-1], series[-1 - lookback]
    return latest[1], latest[1] - earlier[1], latest[0], earlier[1]


def _direction_word(signal: float, up: str, down: str) -> str:
    return up if signal > 0.05 else down if signal < -0.05 else "no clear pull"


# ----------------------------------------------------------------------- builders

def real_yield(rows: Sequence[Tuple[date, Dict[str, float]]], asset: str) -> Optional[Evidence]:
    """Falling real yields lift gold and silver (they cost less to hold)."""
    series = [(d, v["TC_10YEAR"]) for d, v in rows if "TC_10YEAR" in v]
    found = _change(series)
    if not found:
        return None
    level, move, when, _ = found
    signal = clamp(-move / 0.20)
    return Evidence(
        "real_yield", "US 10-year real yield", CONFIRMED, "US Treasury", _stamp(when),
        f"{level:.2f}% ({move:+.2f} pt over {LOOKBACK} trading days)", signal, SCORED_SPECS[asset]["real_yield"],
        DAILY_HOURS, reason="a 0.20 pt fall is fully bullish for a non-yielding metal, a rise bearish",
    )


def us2y(rows: Sequence[Tuple[date, Dict[str, float]]], asset: str) -> Optional[Evidence]:
    """The 2-year yield is the market's rate expectation: falling is friendlier to gold."""
    series = [(d, v["BC_2YEAR"]) for d, v in rows if "BC_2YEAR" in v]
    found = _change(series)
    if not found:
        return None
    level, move, when, _ = found
    return Evidence(
        "us2y", "US 2-year yield (rate expectations)", CONFIRMED, "US Treasury", _stamp(when),
        f"{level:.2f}% ({move:+.2f} pt over {LOOKBACK} trading days)", clamp(-move / 0.25),
        SCORED_SPECS[asset]["us2y"], DAILY_HOURS,
        reason="falling rate expectations help gold; a 0.25 pt move is fully scored",
    )


def dollar(eurusd: Sequence[Tuple[date, float]], asset: str) -> Optional[Evidence]:
    """A weaker dollar (EUR/USD up) lifts gold, silver and oil, which are priced in dollars."""
    found = _change(eurusd)
    if not found:
        return None
    level, move, when, earlier = found
    pct = move / earlier * 100.0 if earlier else 0.0
    return Evidence(
        "usd", "US dollar (via the ECB EUR/USD reference rate)", CONFIRMED, "ECB", _stamp(when),
        f"EUR/USD {level:.4f} ({pct:+.2f}% over {LOOKBACK} days)", clamp(pct / 1.5),
        SCORED_SPECS[asset]["usd"], DAILY_HOURS,
        reason="euro up = dollar down, which supports dollar-priced commodities; 1.5% is fully scored",
    )


def yield_spread(us: Sequence[Tuple[date, float]], de: Sequence[Tuple[date, float]], key: str,
                 label: str, scale: float) -> Optional[Evidence]:
    """US minus German yield. A widening spread favours the dollar: bearish EUR/USD."""
    german = dict(de)
    paired = [(d, v - german[d]) for d, v in us if d in german]
    found = _change(paired)
    if not found:
        return None
    level, move, when, _ = found
    return Evidence(
        key, label, CONFIRMED, "US Treasury and ECB", _stamp(when),
        f"{level:+.2f} pt ({move:+.2f} pt over {LOOKBACK} days)", clamp(-move / scale),
        SCORED_SPECS["eurusd"][key], DAILY_HOURS,
        reason=f"a widening US lead pulls capital to the dollar; {scale:.2f} pt is fully scored",
    )


def crude_stocks(wpsr: Dict[str, Any], asset: str, key: str, row: str, label: str, scale: float
                 ) -> Optional[Evidence]:
    """A stock draw is bullish for oil, a build bearish."""
    data = (wpsr.get("stocks") or {}).get(row)
    if not data or data.get("change") is None:
        return None
    change = data["change"]
    return Evidence(
        key, label, CONFIRMED, "EIA Weekly Petroleum Status Report", _stamp(wpsr["week_ending"]),
        f"{data['now']:.1f} Mb ({change:+.1f} Mb on the week)", clamp(-change / scale),
        SCORED_SPECS[asset][key], WEEKLY_HOURS,
        reason=f"a draw is bullish and a build bearish; {scale:g} Mb is fully scored "
               "(no seasonal norm or consensus is available free)",
    )


def gas_storage(gas: Dict[str, Any]) -> Optional[Evidence]:
    """Storage above the 5-year average is bearish for gas, below it bullish."""
    if gas.get("avg5_pct") is None or not gas.get("week_ending"):
        return None
    pct = gas["avg5_pct"]
    published = gas.get("released") or gas["week_ending"]
    return Evidence(
        "gas_storage", "US natural gas in storage vs 5-year average", CONFIRMED, "EIA Weekly Natural Gas Storage",
        _stamp(published), f"{gas['stocks']:,.0f} Bcf, {pct:+.1f}% vs the 5-year average "
        f"(net change {gas['net_change']:+.0f} Bcf)", clamp(-pct / 6.0), SCORED_SPECS["natgas"]["gas_storage"],
        WEEKLY_HOURS, reason="surplus to normal is bearish, deficit bullish; 6% is fully scored",
    )


# --------------------------------------------------------------------- context only

def positioning(asset: str, books: Dict[str, Dict[str, Any]]) -> Optional[Evidence]:
    kind, code = COT[asset]
    row = books.get(code)
    if not row or not row["oi"]:
        return None
    net = row["long"] - row["short"]
    change = row["chg_long"] - row["chg_short"]
    who = "managed money" if kind == "disagg" else "leveraged funds"
    return Evidence(
        "positioning", f"Speculative positioning ({who})", CONFIRMED, "CFTC Commitments of Traders",
        _stamp(row["date"]), f"net {net:+,.0f} contracts ({net / row['oi'] * 100:+.1f}% of open interest), "
        f"{change:+,.0f} on the week", None, 0.0, POSITIONING_HOURS,
        reason="context: can mean a crowded trade or a trend, so it is not scored",
    )


def policy_rates(effr: Sequence[Tuple[date, float]], dfr: Sequence[Tuple[date, float]]) -> Optional[Evidence]:
    if not effr or not dfr:
        return None
    return Evidence(
        "policy_rates", "Policy rates: Fed funds vs ECB deposit", CONFIRMED, "NY Fed and ECB",
        _stamp(effr[-1][0]), f"Fed funds {effr[-1][1]:.2f}% vs ECB {dfr[-1][1]:.2f}% "
        f"(gap {effr[-1][1] - dfr[-1][1]:+.2f} pt)", None, 0.0, WEEKLY_HOURS * 4,
        reason="context: a level, not a change; the yield-spread rows carry the move",
    )


def fred_context(series: Dict[str, Sequence[Tuple[date, float]]]) -> List[Evidence]:
    """Inflation and jobs, when a free FRED key is set. Context: no free consensus to compare with."""
    labels = {
        "CPIAUCSL": ("US CPI inflation, year on year", "{v:.1f}%", 60 * 24.0),
        "UNRATE": ("US unemployment rate", "{v:.1f}%", 60 * 24.0),
        "PAYEMS": ("US nonfarm payrolls, monthly change", "{v:+,.0f} thousand", 60 * 24.0),
        "DTWEXBGS": ("US dollar, broad trade-weighted index", "{v:.2f}", DAILY_HOURS),
    }
    out = []
    for sid, rows in series.items():
        if sid not in labels or not rows:
            continue
        label, fmt, age = labels[sid]
        out.append(Evidence(
            f"fred_{sid.lower()}", label, CONFIRMED, "FRED (St. Louis Fed)", _stamp(rows[-1][0]),
            fmt.format(v=rows[-1][1]), None, 0.0, age,
            reason="context: no free consensus forecast, so no 'surprise' is scored"))
    return out


def weather(hdd: Optional[float], cities: int, updated: Optional[datetime]) -> Optional[Evidence]:
    if hdd is None:
        return None
    return Evidence(
        "weather", "Gas-demand weather: 7-day forecast heating degree-days", CONFIRMED, "NOAA National Weather Service",
        updated, f"{hdd / cities:.0f} degree-days per city over 7 days ({cities} cities; "
        "65F minus the daily mean)", None, 0.0, 24.0,
        reason="context: cold raises heating demand; no free climate normal to compare with, so not scored",
    )


# ----------------------------------------------------------------------- scheduled

def next_wpsr(now: datetime) -> Evidence:
    """The EIA weekly petroleum report: Wednesdays 10:30 US Eastern (holidays can shift it)."""
    day_ = now.astimezone(UTC).date()
    for ahead in range(0, 8):
        candidate = parsers.eastern_to_utc(day_ + timedelta(days=ahead), 10, 30)
        if candidate.weekday() == 2 and candidate > now:
            break
    return Evidence(
        "next_wpsr", "Next EIA petroleum stocks report", SCHEDULED, "EIA schedule (standard weekly slot)",
        candidate, f"{candidate:%a %d %b %H:%M} UTC", None, 0.0, 24.0 * 8,
        reason="scheduled: expect volatility around the release",
    )


def next_gas(gas: Dict[str, Any]) -> Optional[Evidence]:
    nxt = gas.get("next_release")
    if not nxt:
        return None
    when = parsers.eastern_to_utc(nxt, 10, 30)
    return Evidence(
        "next_gas", "Next EIA natural gas storage report", SCHEDULED, "EIA (stated in the last release)",
        when, f"{when:%a %d %b %H:%M} UTC", None, 0.0, 24.0 * 8,
        reason="scheduled: expect volatility around the release",
    )


def calendar_events(events: Sequence[Dict[str, Any]], asset: str, now: datetime,
                    horizon_hours: float = 48.0) -> List[Evidence]:
    """Optional, unofficial economic calendar: high-impact USD/EUR events only."""
    wanted = {"USD", "EUR"} if asset == "eurusd" else {"USD"}
    out = []
    for event in events:
        if event["country"] not in wanted or event["impact"] != "High":
            continue
        if not now - timedelta(hours=1) <= event["when"] <= now + timedelta(hours=horizon_hours):
            continue
        detail = f"forecast {event['forecast'] or '-'}, previous {event['previous'] or '-'}"
        out.append(Evidence(
            f"cal_{event['when']:%Y%m%d%H%M}_{event['title'][:12]}", f"{event['country']} {event['title']}",
            SCHEDULED, "economic calendar (unofficial community feed)", event["when"],
            f"{event['when']:%a %d %b %H:%M} UTC; {detail}", None, 0.0, horizon_hours + 1.0,
            reason="scheduled and unofficial: times can be wrong"))
    return out[:4]
