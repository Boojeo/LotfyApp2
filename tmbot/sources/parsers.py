"""Pure parsers for the free sources.  Text in, plain data out: no network, no clock.

Each is tested against a real response saved under tests/fixtures/sources.
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

UTC = timezone.utc


def num(text: Any) -> Optional[float]:
    """'3,862' -> 3862.0, '' / '.' / None -> None."""
    if text is None:
        return None
    cleaned = str(text).strip().replace(",", "").replace('"', "")
    if cleaned in ("", ".", "NA", "n/a"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def day(text: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(text).strip()[:10])
    except ValueError:
        return None


# ------------------------------------------------------------------ US Treasury

def parse_treasury(xml_text: str) -> List[Tuple[date, Dict[str, float]]]:
    """Daily yield-curve rows, oldest first: [(date, {"BC_10YEAR": 4.1, ...})]."""
    rows: List[Tuple[date, Dict[str, float]]] = []
    root = ET.fromstring(xml_text.lstrip("﻿").encode("utf-8"))
    for entry in root.iter():
        if _local(entry.tag) != "properties":
            continue
        values: Dict[str, float] = {}
        when: Optional[date] = None
        for child in entry:
            name = _local(child.tag)
            if name == "NEW_DATE":
                when = day(child.text or "")
            else:
                number = num(child.text)
                if number is not None:
                    values[name] = number
        if when and values:
            rows.append((when, values))
    return sorted(rows, key=lambda row: row[0])


# -------------------------------------------------------------------------- ECB

def parse_ecb_csv(text: str) -> List[Tuple[date, float]]:
    """(period, value) pairs, oldest first, from an SDMX ``format=csvdata`` response."""
    out: List[Tuple[date, float]] = []
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        when, value = day(row.get("TIME_PERIOD", "")), num(row.get("OBS_VALUE"))
        if when and value is not None:
            out.append((when, value))
    return sorted(out)


# ------------------------------------------------------------------- NY Fed EFFR

def parse_effr(text: str) -> List[Tuple[date, float]]:
    data = json.loads(text)
    out = []
    for row in data.get("refRates", []):
        when, rate = day(row.get("effectiveDate", "")), num(row.get("percentRate"))
        if when and rate is not None:
            out.append((when, rate))
    return sorted(out)


# ------------------------------------------------------------------- EIA storage

_MONTHS = {m: i for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"), start=1)}


def _long_date(text: str) -> Optional[date]:
    found = re.search(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})", text)
    if found and found.group(1) in _MONTHS:
        return date(int(found.group(3)), _MONTHS[found.group(1)], int(found.group(2)))
    return None


def parse_eia_gas(text: str) -> Optional[Dict[str, Any]]:
    """Lower-48 working gas in storage, with the 5-year comparison (Bcf)."""
    lines = text.lstrip("﻿").splitlines()
    out: Dict[str, Any] = {"released": None, "week_ending": None, "next_release": None}
    for line in lines[:6]:
        if "Released:" in line:
            out["released"] = _long_date(line.split("for the Week")[0])
            if "Week Ending" in line:
                out["week_ending"] = _long_date(line.split("Week Ending")[1])
        elif "Next Release" in line:
            out["next_release"] = _long_date(line)
    for row in csv.reader(lines):
        if row and row[0].strip() == "Total":
            values = [num(cell) for cell in row[1:] if cell.strip() != ""]
            values = [v for v in values if v is not None]
            if len(values) >= 8:
                keys = ("stocks", "prev_stocks", "net_change", "implied_flow", "year_ago",
                        "year_ago_pct", "avg5", "avg5_pct")
                out.update(zip(keys, values[:8]))
                return out
    return None


# ------------------------------------------------------------------- EIA petroleum

def parse_eia_wpsr(text: str) -> Optional[Dict[str, Any]]:
    """Weekly petroleum stocks (million barrels): this week, last week, change."""
    rows = list(csv.reader(text.lstrip("﻿").splitlines()))
    if not rows or rows[0][0] != "STUB_1":
        return None

    def stamp(cell: str) -> Optional[date]:
        found = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2})", cell.strip())
        return date(2000 + int(found.group(3)), int(found.group(1)), int(found.group(2))) if found else None

    out: Dict[str, Any] = {"week_ending": stamp(rows[0][1]), "stocks": {}}
    for row in rows[1:]:
        if len(row) >= 4 and num(row[1]) is not None:
            out["stocks"][row[0].strip()] = {"now": num(row[1]), "prev": num(row[2]), "change": num(row[3])}
    return out if out["week_ending"] and out["stocks"] else None


# ------------------------------------------------------------------------- CFTC

def parse_cftc(text: str, kind: str) -> Dict[str, Dict[str, Any]]:
    """Speculative positioning by contract code.

    ``disagg`` (commodities): managed money long/short and their weekly change.
    ``fin`` (currencies): leveraged funds long/short and their weekly change.
    Layout (from the CFTC file description, checked against a real file): open
    interest at column 7 and the position columns after it; the weekly changes
    start at column 55 for commodities (which repeat the block for Old/Other
    categories first) and at column 24 for currencies.
    """
    long_i, short_i = (13, 14) if kind == "disagg" else (14, 15)
    out: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        row = [cell.strip() for cell in next(csv.reader([line]))]
        if len(row) < 64:
            continue
        when = day(row[2])
        shift = 48 if kind == "disagg" else 17     # first column of the weekly-change block, less 7
        values = [num(row[i]) for i in (7, long_i, short_i, shift + long_i, shift + short_i)]
        if when is None or None in values:
            continue
        oi, long_, short_, chg_long, chg_short = values
        out[row[3]] = {"name": row[0], "date": when, "oi": oi, "long": long_, "short": short_,
                       "chg_long": chg_long, "chg_short": chg_short}
    return out


# -------------------------------------------------------------------------- RSS

def _when(text: str) -> Optional[datetime]:
    try:
        moment = parsedate_to_datetime(re.sub(r"\s+", " ", text.strip()))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_rss(text: str) -> List[Dict[str, Any]]:
    root = ET.fromstring(text.lstrip("﻿").encode("utf-8"))
    items = []
    for item in root.iter("item"):
        fields = {_local(child.tag): (child.text or "").strip() for child in item}
        published = _when(fields.get("pubDate", ""))
        if fields.get("title") and published:
            items.append({"title": re.sub(r"\s+", " ", fields["title"]), "link": fields.get("link", ""),
                          "summary": re.sub(r"<[^>]+>", " ", fields.get("description", ""))[:300],
                          "published": published})
    return items


# -------------------------------------------------------------------------- NWS

def parse_nws_forecast(text: str) -> Optional[Dict[str, Any]]:
    props = json.loads(text).get("properties", {})
    periods = []
    for period in props.get("periods", []):
        try:
            start = datetime.fromisoformat(period["startTime"])
            temperature = float(period["temperature"])
        except (KeyError, ValueError, TypeError):
            continue
        if period.get("temperatureUnit") == "C":
            temperature = temperature * 9 / 5 + 32
        periods.append((start, bool(period.get("isDaytime")), temperature))
    updated = props.get("updateTime") or props.get("generatedAt")
    try:
        stamp = datetime.fromisoformat(updated) if updated else None
    except ValueError:
        stamp = None
    return {"updated": stamp, "periods": periods} if periods else None


def heating_degree_days(periods: List[Tuple[datetime, bool, float]], days: int = 7) -> Optional[float]:
    """Sum of max(0, 65 - daily mean) over the first ``days`` days that have a day and a night."""
    by_day: Dict[date, List[float]] = {}
    for start, _is_day, temperature in periods:
        by_day.setdefault(start.date(), []).append(temperature)
    complete = [sorted(by_day)[i] for i in range(len(by_day)) if len(by_day[sorted(by_day)[i]]) >= 2]
    chosen = complete[:days]
    if len(chosen) < min(days, 5):
        return None
    return sum(max(0.0, 65.0 - sum(by_day[d]) / len(by_day[d])) for d in chosen)


# ------------------------------------------------------------------ FRED / calendar

def parse_fred(text: str) -> List[Tuple[date, float]]:
    out = []
    for row in json.loads(text).get("observations", []):
        when, value = day(row.get("date", "")), num(row.get("value"))
        if when and value is not None:
            out.append((when, value))
    return sorted(out)


def parse_calendar(text: str) -> List[Dict[str, Any]]:
    out = []
    for row in json.loads(text):
        try:
            when = datetime.fromisoformat(row["date"]).astimezone(UTC)
        except (KeyError, ValueError):
            continue
        out.append({"title": row.get("title", ""), "country": row.get("country", ""), "when": when,
                    "impact": row.get("impact", ""), "forecast": row.get("forecast", ""),
                    "previous": row.get("previous", "")})
    return sorted(out, key=lambda r: r["when"])


# ----------------------------------------------------------------- US Eastern time

def eastern_offset(moment: datetime) -> timedelta:
    """US Eastern offset (EST -5 / EDT -4) without needing a time-zone database."""
    year = moment.year

    def nth_sunday(month: int, n: int) -> date:
        first = date(year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))

    start = datetime.combine(nth_sunday(3, 2), datetime.min.time(), tzinfo=UTC) + timedelta(hours=7)
    end = datetime.combine(nth_sunday(11, 1), datetime.min.time(), tzinfo=UTC) + timedelta(hours=6)
    return timedelta(hours=-4) if start <= moment.astimezone(UTC) < end else timedelta(hours=-5)


def eastern_to_utc(day_: date, hour: int, minute: int = 0) -> datetime:
    guess = datetime(day_.year, day_.month, day_.day, hour, minute, tzinfo=UTC)
    return guess - eastern_offset(guess + timedelta(hours=5))
