"""Fetch the free sources, cache them politely, and turn them into one fundamental result.

``SourceService.collect(asset)`` returns the dict the shadow analysis shows under
"Fundamental".  Rules:

* Official numbers are scored (see ``rules``); breaking news, unverified reports,
  scheduled events and context are shown with their source and age and never
  moved a score.
* A source that fails, or data older than its limit, is listed as missing or
  stale, lowers the data-quality figure, and adds no direction.
* Everything is cached (a few hours for daily/weekly data, minutes for news) and a
  failure is remembered for a few minutes, so the sources are not hammered.

Direction and strength:
    weight_i = base weight x freshness_i      (1 up to half the item's age limit,
                                               falling to 0 at the limit)
    score    = 100 x sum(weight_i x signal_i) / sum(weight_i)       (-100..+100)
    direction = bullish / bearish beyond +-20, otherwise neutral; strength = |score|
    coverage = scored weight present / scored weight expected for the instrument
    quality  = reliability (1.0 for official data) x freshness x coverage
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

from ..config import SourcesConfig
from ..models import utcnow
from . import parsers, rules
from .rules import BREAKING, CONFIRMED, SCHEDULED, UNVERIFIED, Evidence

log = logging.getLogger(__name__)
UTC = timezone.utc

DIRECTION_THRESHOLD = 20.0
OFFICIAL_RELIABILITY = 1.0
FAILURE_RETRY_SECONDS = 300.0
USER_AGENT = "tmbot/1.0 (personal trading assistant; reads free public data)"

# seconds a successful fetch is reused
TTL = {"daily": 3 * 3600.0, "weekly": 3 * 3600.0, "positioning": 12 * 3600.0, "weather": 3 * 3600.0,
       "news": 15 * 60.0, "calendar": 3600.0, "fred": 6 * 3600.0}

TREASURY = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
            "?data={data}&field_tdr_date_value_month={month}")
ECB = "https://data-api.ecb.europa.eu/service/data/{flow}/{key}?lastNObservations={n}&format=csvdata"
URLS = {
    "effr": "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/10.json",
    "eia_gas": "https://ir.eia.gov/ngs/wngsr.csv",
    "wpsr": "https://ir.eia.gov/wpsr/table1.csv",
    "cot_disagg": "https://www.cftc.gov/dea/newcot/f_disagg.txt",
    "cot_fin": "https://www.cftc.gov/dea/newcot/FinFutWk.txt",
    "calendar": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "nws_point": "https://api.weather.gov/points/{lat},{lon}",
    "fred": "https://api.stlouisfed.org/fred/series/observations?series_id={sid}&api_key={key}"
            "&file_type=json&sort_order=desc&limit=14{units}",
}
# four large gas-demand cities for the weather context
CITIES = {"New York": (40.71, -74.01), "Chicago": (41.88, -87.63), "Boston": (42.36, -71.06),
          "Philadelphia": (39.95, -75.17)}

# name, url, tier, which assets, how long a headline stays relevant
FEEDS: Tuple[Tuple[str, str, str, Tuple[str, ...], float], ...] = (
    ("Federal Reserve press releases", "https://www.federalreserve.gov/feeds/press_monetary.xml",
     BREAKING, ("gold", "silver", "eurusd", "wti", "brent"), 72.0),
    ("European Central Bank press", "https://www.ecb.europa.eu/rss/press.html", BREAKING,
     ("eurusd", "gold", "silver"), 72.0),
    ("EIA Today in Energy", "https://www.eia.gov/rss/todayinenergy.xml", BREAKING,
     ("wti", "brent", "natgas"), 72.0),
    ("Oilprice.com", "https://oilprice.com/rss/main", UNVERIFIED, ("wti", "brent", "natgas"), 24.0),
    ("MarketWatch MarketPulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", UNVERIFIED,
     ("gold", "silver", "eurusd", "wti", "brent", "natgas"), 24.0),
)
KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "gold": ("gold", "bullion", "safe haven", "yield", "treasur", "inflation", "dollar", "fed ", "rate cut", "rate hike"),
    "silver": ("silver", "gold", "bullion", "yield", "inflation", "dollar", "fed "),
    "wti": ("oil", "crude", "opec", "wti", "barrel", "refin", "sanction", "pipeline", "energy", "supply"),
    "brent": ("oil", "crude", "opec", "brent", "barrel", "refin", "sanction", "pipeline", "energy", "supply"),
    "natgas": ("natural gas", "lng", "henry hub", "gas storage", "gas price", "gas demand", "pipeline"),
    "eurusd": ("euro", "ecb", "eur/usd", "dollar", "fed ", "inflation", "interest rate", "eurozone", "lagarde"),
}
LABELS = {
    "real_yield": "US 10-year real yield", "us2y": "US 2-year yield", "usd": "US dollar proxy (ECB EUR/USD)",
    "crude": "US commercial crude stocks", "gasoline": "US gasoline stocks", "distillate": "US distillate stocks",
    "gas_storage": "US natural gas storage", "spread2": "US-Germany 2-year yield spread",
    "spread10": "US-Germany 10-year yield spread",
}
# which fetch each scored item depends on (to say WHY one is missing)
SPEC_SOURCES = {
    "real_yield": ("treasury_real",), "us2y": ("treasury_nominal",), "usd": ("ecb_exr",),
    "crude": ("wpsr",), "gasoline": ("wpsr",), "distillate": ("wpsr",), "gas_storage": ("eia_gas",),
    "spread2": ("treasury_nominal", "ecb_2y"), "spread10": ("treasury_nominal", "ecb_10y"),
}
NEEDS = {
    "gold": ("treasury_real", "treasury_nominal", "ecb_exr", "cot"),
    "silver": ("treasury_real", "treasury_nominal", "ecb_exr", "cot"),
    "wti": ("wpsr", "ecb_exr", "cot"), "brent": ("wpsr", "ecb_exr", "cot"),
    "natgas": ("eia_gas", "weather", "cot"),
    "eurusd": ("treasury_nominal", "ecb_curve", "effr", "cot"),
}


class SourceService:
    """Never blocks its caller: a report reads what is cached and any download runs in the background.

    * first call for a source: starts the download in a daemon thread and reports it as "still loading";
    * cached and younger than its TTL: used as it is;
    * older than its TTL: the last good copy is used while one background refresh runs (at most one
      per source, however many reports ask); if the refresh fails the last good copy stays and the
      failure is noted;
    * a failure with nothing to fall back on is remembered for a few minutes, not retried every call.
    Whether data is fresh is decided from the date INSIDE the data (when it was published or
    observed), never from when it was downloaded, so a cached copy cannot pass for fresh.
    """

    def __init__(self, config: SourcesConfig, session: Optional[requests.Session] = None,
                 clock: Callable[[], datetime] = utcnow):
        self.config = config
        self._http = session or requests.Session()
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (stored at, value, error); value is the last GOOD copy (kept through failures)
        self._cache: Dict[str, Tuple[float, Any, str]] = {}
        self._running: Dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------- fetching

    def _get(self, url: str, **kwargs: Any) -> str:
        """One GET, retried once if the server is slow or drops the connection."""
        headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
        for attempt in (1, 2):
            try:
                response = self._http.get(url, timeout=self.config.timeout, headers=headers, **kwargs)
                response.raise_for_status()
                return response.content.decode("utf-8", errors="replace")
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def _refresh(self, key: str, ttl: float, load: Callable[[], Any]) -> None:
        """Download ``key`` in a daemon thread, at most one at a time per key.

        The cache is looked at again under the lock: a download that finished a moment ago
        must not be started a second time by a caller that decided just before it landed.
        """
        with self._lock:
            running = self._running.get(key)
            if running is not None and running.is_alive():
                return
            entry = self._cache.get(key)
            if entry and time.monotonic() - entry[0] < (FAILURE_RETRY_SECONDS if entry[2] else ttl):
                return

            def work() -> None:
                try:
                    value, error = load(), ""
                except Exception as exc:
                    value, error = None, f"{type(exc).__name__}: {str(exc)[:120]}"
                    log.warning("free source %s failed: %s", key, error)
                with self._lock:
                    previous = self._cache.get(key)
                    if error and previous and previous[1] is not None:
                        value = previous[1]                      # keep the last good copy
                    self._cache[key] = (time.monotonic(), value, error)
                    self._running.pop(key, None)

            thread = threading.Thread(target=work, name=f"source-{key}", daemon=True)
            self._running[key] = thread
            thread.start()

    def _cached(self, key: str, ttl: float, load: Callable[[], Any]) -> Tuple[Any, str]:
        """(value, error) at once. Starts a background download when there is none or it is old."""
        with self._lock:
            entry = self._cache.get(key)
        if entry:
            stored, value, error = entry
            limit = FAILURE_RETRY_SECONDS if error else ttl
            if time.monotonic() - stored < limit:
                return value, error
            self._refresh(key, ttl, load)
            return value, (error if value is None else "")
        self._refresh(key, ttl, load)
        return None, "still loading (first download in progress); used from the next report"

    def loading(self) -> bool:
        with self._lock:
            return any(t.is_alive() for t in self._running.values())

    def wait_idle(self, seconds: float) -> bool:
        """For the command line and tests only: wait for the downloads to finish."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.loading():
                return True
            time.sleep(0.05)
        return not self.loading()

    def _treasury(self, data: str) -> List[Tuple[Any, Dict[str, float]]]:
        """This month's rows, plus last month's when it is early in the month (a 5-day change needs 6)."""
        month = self._clock().replace(day=1)
        rows = parsers.parse_treasury(self._get(TREASURY.format(data=data, month=f"{month:%Y%m}")))
        if len(rows) < 8:
            earlier = (month - timedelta(days=1)).replace(day=1)
            rows += parsers.parse_treasury(self._get(TREASURY.format(data=data, month=f"{earlier:%Y%m}")))
        return sorted(rows, key=lambda row: row[0])

    def _ecb(self, flow: str, key: str, n: int = 12) -> List[Tuple[Any, float]]:
        return parsers.parse_ecb_csv(self._get(ECB.format(flow=flow, key=key, n=n)))

    def _loader(self, name: str) -> Tuple[float, Callable[[], Any]]:
        daily, weekly = TTL["daily"], TTL["weekly"]
        table: Dict[str, Tuple[float, Callable[[], Any]]] = {
            "treasury_real": (daily, lambda: self._treasury("daily_treasury_real_yield_curve")),
            "treasury_nominal": (daily, lambda: self._treasury("daily_treasury_yield_curve")),
            "ecb_exr": (daily, lambda: self._ecb("EXR", "D.USD.EUR.SP00.A")),
            "ecb_2y": (daily, lambda: self._ecb("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y")),
            "ecb_10y": (daily, lambda: self._ecb("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y")),
            "ecb_dfr": (weekly, lambda: self._ecb("FM", "B.U2.EUR.4F.KR.DFR.LEV", 3)),
            "effr": (weekly, lambda: parsers.parse_effr(self._get(URLS["effr"]))),
            "eia_gas": (weekly, lambda: parsers.parse_eia_gas(self._get(URLS["eia_gas"]))),
            "wpsr": (weekly, lambda: parsers.parse_eia_wpsr(self._get(URLS["wpsr"]))),
            "cot_disagg": (TTL["positioning"], lambda: parsers.parse_cftc(self._get(URLS["cot_disagg"]), "disagg")),
            "cot_fin": (TTL["positioning"], lambda: parsers.parse_cftc(self._get(URLS["cot_fin"]), "fin")),
        }
        return table[name]

    def _fetch(self, name: str) -> Tuple[Any, str]:
        ttl, load = self._loader(name)
        return self._cached(name, ttl, load)

    def _weather(self) -> Optional[Tuple[float, int, Optional[datetime]]]:
        total, count, updated = 0.0, 0, None
        for city, (lat, lon) in CITIES.items():
            def load(lat=lat, lon=lon):
                point = self._get(URLS["nws_point"].format(lat=lat, lon=lon))
                return parsers.parse_nws_forecast(self._get(json.loads(point)["properties"]["forecast"]))
            data, _ = self._cached(f"nws:{city}", TTL["weather"], load)
            hdd = parsers.heating_degree_days(data["periods"]) if data else None
            if hdd is not None:
                total, count, updated = total + hdd, count + 1, data.get("updated") or updated
        return (total, count, updated) if count else None

    def _fred(self) -> Dict[str, List[Tuple[Any, float]]]:
        key = self.config.fred_api_key
        out: Dict[str, List[Tuple[Any, float]]] = {}
        if not key:
            return out
        for sid, units in (("CPIAUCSL", "&units=pc1"), ("UNRATE", ""), ("PAYEMS", "&units=chg"), ("DTWEXBGS", "")):
            data, _ = self._cached(f"fred:{sid}", TTL["fred"], lambda sid=sid, units=units: parsers.parse_fred(
                self._get(URLS["fred"].format(sid=sid, key=key, units=units))))
            if data:
                out[sid] = data
        return out

    # ----------------------------------------------------------------- evidence

    def _gather(self, asset: str, now: datetime) -> Tuple[List[Evidence], Dict[str, str]]:
        """All evidence for ``asset`` from what is cached, and why any source is missing. Never waits."""
        needs = list(NEEDS[asset])
        if asset == "eurusd":
            needs = ["treasury_nominal", "ecb_2y", "ecb_10y", "ecb_dfr", "effr", "cot"]
        data: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        names = [n for n in needs if n not in ("cot", "weather")]
        if "cot" in needs:
            names.append("cot_fin" if rules.COT[asset][0] == "fin" else "cot_disagg")
        for name in names:
            value, error = self._fetch(name)
            data[name] = value
            if value is None:
                errors[name] = error or "no data returned"
        if "weather" in needs and self.config.weather:
            data["weather"] = self._weather()
        if self.config.fred_api_key:
            data["fred"] = self._fred()
        if self.config.calendar_unofficial:
            events, error = self._cached("calendar", TTL["calendar"],
                                         lambda: parsers.parse_calendar(self._get(URLS["calendar"])))
            data["calendar"] = events
            if events is None:
                errors["calendar"] = error

        out: List[Evidence] = []

        def add(item: Optional[Evidence]) -> None:
            if item:
                out.append(item)

        treasury_real, treasury_nominal, exr = data.get("treasury_real"), data.get("treasury_nominal"), data.get("ecb_exr")
        if asset in ("gold", "silver"):
            add(treasury_real and rules.real_yield(treasury_real, asset))
            add(treasury_nominal and rules.us2y(treasury_nominal, asset))
            add(exr and rules.dollar(exr, asset))
        elif asset in ("wti", "brent"):
            wpsr = data.get("wpsr")
            if wpsr:
                add(rules.crude_stocks(wpsr, asset, "crude", "Commercial (Excluding SPR)",
                                       "US commercial crude oil stocks", 6.0))
                add(rules.crude_stocks(wpsr, asset, "gasoline", "Total Motor Gasoline", "US gasoline stocks", 5.0))
                add(rules.crude_stocks(wpsr, asset, "distillate", "Distillate Fuel Oil", "US distillate stocks", 4.0))
            add(exr and rules.dollar(exr, asset))
            out.append(rules.next_wpsr(now))
        elif asset == "natgas":
            gas = data.get("eia_gas")
            if gas:
                add(rules.gas_storage(gas))
                add(rules.next_gas(gas))
            if data.get("weather"):
                add(rules.weather(*data["weather"]))
        elif asset == "eurusd":
            us2 = [(d, v["BC_2YEAR"]) for d, v in (treasury_nominal or []) if "BC_2YEAR" in v]
            us10 = [(d, v["BC_10YEAR"]) for d, v in (treasury_nominal or []) if "BC_10YEAR" in v]
            if data.get("ecb_2y") and us2:
                add(rules.yield_spread(us2, data["ecb_2y"], "spread2", "US-Germany 2-year yield spread", 0.25))
            if data.get("ecb_10y") and us10:
                add(rules.yield_spread(us10, data["ecb_10y"], "spread10", "US-Germany 10-year yield spread", 0.25))
            add(data.get("effr") and data.get("ecb_dfr") and rules.policy_rates(data["effr"], data["ecb_dfr"]))
        books = data.get("cot_fin") or data.get("cot_disagg")
        if books:
            add(rules.positioning(asset, books))
        out += rules.fred_context(data.get("fred") or {})
        if data.get("calendar"):
            out += rules.calendar_events(data["calendar"], asset, now)
        if self.config.news_feeds:
            items, feed_errors = self._news(asset, now)
            out += items
            errors.update(feed_errors)
        return out, errors

    def _news(self, asset: str, now: datetime) -> Tuple[List[Evidence], Dict[str, str]]:
        words = KEYWORDS[asset]
        found: List[Evidence] = []
        errors: Dict[str, str] = {}
        for name, url, tier, assets, hours in FEEDS:
            if asset not in assets:
                continue
            items, error = self._cached(f"feed:{name}", TTL["news"], lambda url=url: parsers.parse_rss(self._get(url)))
            if items is None:
                errors[f"feed:{name}"] = error or "no data returned"
                continue
            official = tier == BREAKING and ("federalreserve" in url or "ecb.europa" in url)
            for item in items:
                if now - item["published"] > timedelta(hours=hours) or item["published"] > now + timedelta(hours=1):
                    continue
                text = f"{item['title']} {item['summary']}".lower()
                if not official and not any(word in text for word in words):
                    continue
                summary = item["summary"].strip()
                found.append(Evidence(
                    "news", item["title"][:140], tier, name, item["published"],
                    "" if _tokens(summary)[:60] == _tokens(item["title"])[:60] else summary[:140],
                    None, 0.0, hours, item["link"],
                    "official announcement: context only, not scored" if tier == BREAKING
                    else "unverified single-source report: context only, never scored"))
        return dedupe(found), errors

    def warm(self) -> None:
        """Start every download in the background (never blocks); later reports find it cached."""
        for asset in rules.SCORED_SPECS:
            try:
                self._gather(asset, self._clock())
            except Exception:
                log.exception("warming the free sources for %s failed", asset)

    # ---------------------------------------------------------------- aggregation

    def collect(self, asset: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        """The fundamental result for ``asset`` from what is cached. Never waits for a download."""
        now = now or self._clock()
        evidence, errors = self._gather(asset, now)
        return aggregate(asset, evidence, errors, now)


# ------------------------------------------------------------------------- helpers

def _tokens(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def dedupe(items: Sequence[Evidence]) -> List[Evidence]:
    """One entry per story: same link, or titles that are nearly the same. Newest, official first."""
    ordered = sorted(items, key=lambda e: (e.tier != BREAKING, -(e.published_at or datetime.min.replace(tzinfo=UTC)).timestamp()))
    kept: List[Evidence] = []
    for item in ordered:
        text = _tokens(item.label)
        if any((item.url and item.url == k.url) or SequenceMatcher(None, text, _tokens(k.label)).ratio() > 0.85
               for k in kept):
            continue
        kept.append(item)
    return kept[:5]


def freshness(age_hours: float, limit: float) -> float:
    """1 up to half the limit, falling in a straight line to 0 at the limit."""
    if age_hours <= limit / 2:
        return 1.0
    if age_hours >= limit:
        return 0.0
    return (limit - age_hours) / (limit / 2)


def aggregate(asset: str, evidence: Sequence[Evidence], errors: Dict[str, str], now: datetime) -> Dict[str, Any]:
    specs = rules.SCORED_SPECS[asset]
    items: List[Dict[str, Any]] = []
    weighted = total_weight = fresh_sum = base_present = 0.0
    reasons: List[Dict[str, Any]] = []
    present: set = set()
    stale_notes: List[Dict[str, Any]] = []
    event_risk: List[str] = []
    lead = (-1.0, "")
    for e in evidence:
        age = max(0.0, (now - e.published_at).total_seconds() / 3600.0) if e.published_at else None
        scheduled_future = e.tier == SCHEDULED and e.published_at and e.published_at > now
        fresh = 1.0 if scheduled_future else (freshness(age, e.max_age_hours) if age is not None else 0.0)
        counted = e.scored and fresh > 0
        row = {"source": e.source, "title": f"{e.label}: {e.text}" if e.text else e.label, "published_at": e.published_at.isoformat() if e.published_at else "",
               "age_hours": None if age is None or scheduled_future else round(age, 1), "tier": e.tier,
               "used": counted, "skipped": "" if fresh > 0 else ("no_timestamp" if age is None else "stale"),
               "signal": None if e.signal is None else round(e.signal, 2), "weight": e.weight,
               "url": e.url, "why": e.reason}
        items.append(row)
        if e.tier == SCHEDULED and e.published_at and now - timedelta(minutes=30) <= e.published_at <= now + timedelta(hours=3):
            event_risk.append(f"{e.label} at {e.published_at:%H:%M} UTC")
        if e.scored and not counted:
            stale_notes.append({"key": "src_stale", "args": {"what": e.label, "hours": f"{e.max_age_hours:g}"}})
        if counted:
            present.add(e.key)
            base_present += e.weight
            w = e.weight * fresh
            weighted += w * e.signal
            total_weight += w
            fresh_sum += e.weight * fresh
            mood = "bullish" if e.signal > 0.05 else "bearish" if e.signal < -0.05 else "neutral"
            reasons.append({"key": "text", "args": {"text": f"{e.label}: {e.text} → {mood} (weight {e.weight:g}). {e.reason}"}})
            pull = abs(e.signal) * e.weight * fresh
            if pull >= lead[0]:
                lead = (pull, f"{e.label} {e.text.split(' (')[0]} → {mood}")

    expected = sum(specs.values())
    missing: List[Dict[str, Any]] = [
        {"key": "src_missing", "args": {"what": LABELS.get(k, k), "why": next(
            (errors[n] for n in SPEC_SOURCES.get(k, ()) if n in errors), "too few observations")}}
        for k in specs if k not in present and not any(n["args"]["what"] == LABELS.get(k, k) for n in stale_notes)]
    missing += stale_notes
    explained = {n for k in specs for n in SPEC_SOURCES.get(k, ())}      # already said, per item, above
    missing += [{"key": "src_failed", "args": {"name": name, "why": why}}
                for name, why in errors.items() if name not in explained]
    result: Dict[str, Any] = {
        "status": "unavailable", "why": "", "direction": None, "strength": None, "quality": 0.0,
        "reliability": 0.0, "freshness": 0.0, "coverage": 0.0, "n_items": len(items),
        "n_used": sum(1 for r in items if r["used"]), "items": items[:12], "reasons": reasons,
        "risks": [{"key": "text", "args": {"text":
                  "Scored from public official data with provisional scales; no consensus forecasts, "
                  "seasonal norms or news direction are used."}}],
        "missing": missing, "reader": "official data", "provider": "free sources", "asset": asset,
        "score": 0.0, "event_risk": event_risk, "lead": "",
    }
    if total_weight <= 0:
        result["why"] = "no_data"
        return result
    if base_present / expected < 0.5:
        result["risks"].append({"key": "text", "args": {"text": (
            f"Only {base_present / expected * 100:.0f}% of the expected evidence is available: "
            "treat this reading as weak.")}})
    score = 100.0 * weighted / total_weight
    freshness_avg = fresh_sum / base_present if base_present else 0.0
    coverage = min(1.0, base_present / expected)
    direction = "BULLISH" if score >= DIRECTION_THRESHOLD else "BEARISH" if score <= -DIRECTION_THRESHOLD else "NEUTRAL"
    result["lead"] = lead[1]
    result.update(
        status="available", direction=direction, strength=round(min(100.0, abs(score)), 1), score=round(score, 1),
        reliability=OFFICIAL_RELIABILITY, freshness=round(freshness_avg, 2), coverage=round(coverage, 2),
        quality=round(OFFICIAL_RELIABILITY * freshness_avg * coverage, 3),
    )
    return result
