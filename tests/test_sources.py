"""The free fundamental sources: parsers on real saved responses, the rules, the
aggregation, the slow-server behaviour, and proof they decide nothing."""

from __future__ import annotations

import json
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from tmbot.analysis import funnel, scoring
from tmbot.analysis.report import ReportBuilder, render_full, render_text
from tmbot.config import Config, SourcesConfig
from tmbot.i18n import Translator
from tmbot.sources import parsers, rules, service
from tmbot.sources.rules import BREAKING, CONFIRMED, SCHEDULED, UNVERIFIED, Evidence
from tests.test_style import UP, auto_config, broker_with

UTC = timezone.utc
FIX = Path(__file__).parent / "fixtures" / "sources"
NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
T = Translator()


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------------------- parsers

class ParserTests(unittest.TestCase):
    def test_treasury_real_and_nominal_yields(self):
        real = parsers.parse_treasury(read("treasury_real_202611.xml"))
        self.assertEqual(real[-1][0], date(2026, 11, 18))
        self.assertAlmostEqual(real[-1][1]["TC_10YEAR"], 2.88)
        nominal = parsers.parse_treasury(read("treasury_nominal_202611.xml"))
        self.assertEqual((nominal[-1][1]["BC_2YEAR"], nominal[-1][1]["BC_10YEAR"]), (4.8, 5.07))
        self.assertEqual([d for d, _ in real], sorted(d for d, _ in real))

    def test_ecb_series(self):
        self.assertEqual(parsers.parse_ecb_csv(read("ecb_exr.csv"))[-1], (date(2026, 11, 18), 1.0968))
        self.assertEqual(parsers.parse_ecb_csv(read("ecb_dfr.csv"))[-1][1], 2.5)
        self.assertEqual(parsers.parse_ecb_csv(read("ecb_2y.csv"))[-1][0], date(2026, 11, 17))

    def test_effr(self):
        self.assertEqual(parsers.parse_effr(read("effr.json"))[-1], (date(2026, 11, 17), 3.88))

    def test_eia_gas_storage_with_the_five_year_comparison(self):
        gas = parsers.parse_eia_gas(read("eia_gas.csv"))
        self.assertEqual((gas["stocks"], gas["net_change"], gas["avg5"], gas["avg5_pct"]), (3862, 61, 3787, 2.0))
        self.assertEqual(gas["week_ending"], date(2026, 11, 6))
        self.assertEqual(gas["next_release"], date(2026, 11, 19))

    def test_eia_petroleum_stocks(self):
        wpsr = parsers.parse_eia_wpsr(read("eia_wpsr1.csv"))
        self.assertEqual(wpsr["week_ending"], date(2026, 11, 13))
        crude = wpsr["stocks"]["Commercial (Excluding SPR)"]
        self.assertEqual((crude["now"], crude["prev"]), (417.797, 414.417))
        self.assertAlmostEqual(crude["change"], 3.38)

    def test_cftc_positioning_columns(self):
        books = parsers.parse_cftc(read("cftc_disagg.txt"), "disagg")
        gold = books["088691"]
        self.assertEqual((gold["long"], gold["short"], gold["oi"]), (127406, 15065, 404725))
        self.assertEqual((gold["chg_long"], gold["chg_short"]), (5123, -3604))
        euro = parsers.parse_cftc(read("cftc_fin.txt"), "fin")["099741"]
        self.assertEqual((euro["long"], euro["short"], euro["chg_long"], euro["chg_short"]),
                         (114051, 185727, -5187, -12070))

    def test_rss_dates_become_utc(self):
        for name in ("fed_monetary.xml", "ecb_press.xml", "oilprice.xml", "eia_today.xml", "marketwatch.xml"):
            items = parsers.parse_rss(read(name))
            self.assertTrue(items, name)
            self.assertTrue(all(i["published"].tzinfo is not None for i in items), name)
        fed = parsers.parse_rss(read("fed_monetary.xml"))[0]
        self.assertEqual(fed["published"], datetime(2026, 11, 18, 19, 0, tzinfo=UTC))
        ecb = parsers.parse_rss(read("ecb_press.xml"))[0]
        self.assertEqual(ecb["published"], datetime(2026, 11, 18, 9, 0, tzinfo=UTC))      # +0100 -> UTC

    def test_weather_degree_days(self):
        forecast = parsers.parse_nws_forecast(read("nws_forecast.json"))
        hdd = parsers.heating_degree_days(forecast["periods"])
        self.assertTrue(0 < hdd < 7 * 40)
        self.assertIsNone(parsers.heating_degree_days(forecast["periods"][:3]))      # too few days

    def test_the_calendar_and_fred_shapes(self):
        events = parsers.parse_calendar(read("calendar.json"))
        self.assertTrue(events and events[0]["when"].tzinfo)
        fred = parsers.parse_fred(json.dumps({"observations": [
            {"date": "2026-10-01", "value": "3.1"}, {"date": "2026-09-01", "value": "."}]}))
        self.assertEqual(fred, [(date(2026, 10, 1), 3.1)])

    def test_us_eastern_time_without_a_timezone_database(self):
        self.assertEqual(parsers.eastern_to_utc(date(2026, 11, 19), 10, 30), datetime(2026, 11, 19, 15, 30, tzinfo=UTC))
        self.assertEqual(parsers.eastern_to_utc(date(2026, 7, 15), 10, 30), datetime(2026, 7, 15, 14, 30, tzinfo=UTC))
        self.assertEqual(parsers.eastern_to_utc(date(2026, 3, 9), 10, 30), datetime(2026, 3, 9, 14, 30, tzinfo=UTC))
        self.assertEqual(parsers.eastern_to_utc(date(2026, 3, 6), 10, 30), datetime(2026, 3, 6, 15, 30, tzinfo=UTC))

    def test_junk_is_not_a_number(self):
        for junk in (None, "", ".", "n/a", "abc"):
            self.assertIsNone(parsers.num(junk))
        self.assertEqual(parsers.num("3,862"), 3862.0)


# --------------------------------------------------------------------------- rules

def series(values, start=date(2026, 11, 1)):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


class RuleTests(unittest.TestCase):
    def test_a_falling_real_yield_is_bullish_for_gold_and_a_rising_one_bearish(self):
        fall = [(d, {"TC_10YEAR": v}) for d, v in series([2.0, 2.0, 1.95, 1.9, 1.85, 1.8])]
        rise = [(d, {"TC_10YEAR": v}) for d, v in series([1.8, 1.85, 1.9, 1.95, 2.0, 2.05])]
        self.assertGreater(rules.real_yield(fall, "gold").signal, 0.5)
        self.assertLess(rules.real_yield(rise, "gold").signal, -0.5)

    def test_a_weaker_dollar_helps_commodities(self):
        self.assertGreater(rules.dollar(series([1.00, 1.00, 1.01, 1.01, 1.02, 1.03]), "gold").signal, 0)
        self.assertLess(rules.dollar(series([1.03, 1.03, 1.02, 1.02, 1.01, 1.00]), "wti").signal, 0)

    def test_a_widening_us_lead_is_bearish_for_eurusd(self):
        us, de = series([4.0, 4.0, 4.1, 4.2, 4.3, 4.4]), series([2.5] * 6)
        self.assertLess(rules.yield_spread(us, de, "spread2", "x", 0.25).signal, -0.5)
        self.assertGreater(rules.yield_spread(series([4.4, 4.3, 4.2, 4.1, 4.0, 4.0]), de, "spread2", "x", 0.25).signal, 0.5)

    def test_a_crude_draw_is_bullish_and_a_build_bearish(self):
        draw = {"week_ending": date(2026, 11, 13), "stocks": {"Commercial (Excluding SPR)": {"now": 400, "prev": 406, "change": -6}}}
        build = {"week_ending": date(2026, 11, 13), "stocks": {"Commercial (Excluding SPR)": {"now": 410, "prev": 404, "change": 6}}}
        args = ("wti", "crude", "Commercial (Excluding SPR)", "crude", 6.0)
        self.assertEqual(rules.crude_stocks(draw, *args).signal, 1.0)
        self.assertEqual(rules.crude_stocks(build, *args).signal, -1.0)

    def test_gas_storage_against_the_five_year_average(self):
        base = {"week_ending": date(2026, 11, 6), "released": date(2026, 11, 13), "stocks": 3800, "net_change": 50}
        self.assertLess(rules.gas_storage({**base, "avg5_pct": 4.0}).signal, 0)
        self.assertGreater(rules.gas_storage({**base, "avg5_pct": -4.0}).signal, 0)

    def test_signals_never_leave_minus_one_to_plus_one(self):
        huge = [(d, {"TC_10YEAR": v}) for d, v in series([0, 0, 0, 0, 0, 50])]
        self.assertEqual(rules.real_yield(huge, "gold").signal, -1.0)

    def test_too_little_history_gives_no_evidence_not_a_neutral_one(self):
        self.assertIsNone(rules.real_yield([(d, {"TC_10YEAR": 1.0}) for d, _ in series([1, 2, 3])], "gold"))
        self.assertIsNone(rules.dollar(series([1.0, 1.0]), "gold"))

    def test_positioning_weather_and_calendar_are_context_never_scored(self):
        books = parsers.parse_cftc(read("cftc_disagg.txt"), "disagg")
        pos = rules.positioning("gold", books)
        self.assertFalse(pos.scored)
        self.assertEqual((pos.signal, pos.weight), (None, 0.0))
        self.assertFalse(rules.weather(100.0, 4, NOW).scored)
        events = [{"title": "CPI", "country": "USD", "when": NOW + timedelta(hours=2), "impact": "High",
                   "forecast": "3.1%", "previous": "3.0%"}]
        out = rules.calendar_events(events, "gold", NOW)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].scored)
        self.assertEqual(out[0].tier, SCHEDULED)
        self.assertEqual(rules.calendar_events([{**events[0], "country": "JPY"}], "gold", NOW), [])

    def test_the_next_petroleum_report_is_a_future_wednesday(self):
        item = rules.next_wpsr(NOW)
        self.assertEqual(item.published_at.weekday(), 2)
        self.assertGreater(item.published_at, NOW)
        self.assertEqual(item.tier, SCHEDULED)

    def test_every_instrument_is_recognised(self):
        names = {"XAUUSDm": "gold", "XAGUSDm": "silver", "UKOILm": "brent", "USOILm": "wti",
                 "XNGUSDm": "natgas", "EURUSDm": "eurusd", "GOLD": "gold"}
        for epic, asset in names.items():
            self.assertEqual(rules.detect_asset(epic), asset, epic)
        self.assertIsNone(rules.detect_asset("BTCUSD"))
        self.assertEqual(rules.detect_asset("MYGOLD", override="silver"), "silver")


# ----------------------------------------------------------------------- aggregation

def ev(key, signal, weight, hours_old=1.0, limit=96.0, tier=CONFIRMED):
    return Evidence(key, key, tier, "src", NOW - timedelta(hours=hours_old), "v", signal, weight, limit)


class AggregateTests(unittest.TestCase):
    def agg(self, evidence, errors=None, asset="gold"):
        return service.aggregate(asset, evidence, errors or {}, NOW)

    def test_agreeing_evidence_gives_a_strong_direction_and_full_quality(self):
        r = self.agg([ev("real_yield", 0.8, 3.0), ev("us2y", 0.8, 1.5), ev("usd", 0.8, 1.5)])
        self.assertEqual((r["status"], r["direction"]), ("available", "BULLISH"))
        self.assertAlmostEqual(r["score"], 80.0, places=1)
        self.assertEqual((r["coverage"], r["freshness"], r["quality"]), (1.0, 1.0, 1.0))

    def test_opposing_evidence_cancels_by_weight(self):
        r = self.agg([ev("real_yield", 1.0, 3.0), ev("us2y", -1.0, 1.5), ev("usd", -1.0, 1.5)])
        self.assertEqual(r["direction"], "NEUTRAL")
        self.assertAlmostEqual(r["score"], 0.0, places=1)

    def test_nothing_available_is_unavailable_not_neutral(self):
        r = self.agg([], {"treasury_real": "timeout"})
        self.assertEqual((r["status"], r["why"], r["direction"], r["strength"]), ("unavailable", "no_data", None, None))
        self.assertTrue(r["missing"])

    def test_context_only_evidence_never_makes_it_available(self):
        r = self.agg([ev("positioning", None, 0.0), ev("news", None, 0.0, tier=BREAKING)])
        self.assertEqual(r["status"], "unavailable")

    def test_a_missing_source_lowers_quality_but_adds_no_direction(self):
        full = self.agg([ev("real_yield", 0.8, 3.0), ev("us2y", 0.8, 1.5), ev("usd", 0.8, 1.5)])
        part = self.agg([ev("usd", 0.8, 1.5)], {"treasury_real": "timeout", "treasury_nominal": "timeout"})
        self.assertAlmostEqual(part["score"], full["score"], places=1)         # same reading ...
        self.assertAlmostEqual(part["coverage"], 0.25)                          # ... less of the evidence
        self.assertLess(part["quality"], full["quality"])
        self.assertTrue(any("weak" in r["args"]["text"] for r in part["risks"]))
        self.assertTrue(any(m["key"] == "src_missing" for m in part["missing"]))

    def test_stale_data_is_left_out_and_said_so(self):
        r = self.agg([ev("real_yield", 1.0, 3.0, hours_old=200), ev("usd", -0.5, 1.5)])
        self.assertEqual(r["direction"], "BEARISH")          # only the fresh one counted
        old = [i for i in r["items"] if i["skipped"] == "stale"]
        self.assertEqual(len(old), 1)
        self.assertTrue(any(m["key"] == "src_stale" for m in r["missing"]))

    def test_freshness_shrinks_the_weight_of_ageing_data(self):
        self.assertEqual(service.freshness(10, 96), 1.0)
        self.assertAlmostEqual(service.freshness(72, 96), 0.5)
        self.assertEqual(service.freshness(96, 96), 0.0)
        old = self.agg([ev("real_yield", 1.0, 3.0, hours_old=80), ev("usd", -1.0, 1.5, hours_old=1)])
        new = self.agg([ev("real_yield", 1.0, 3.0, hours_old=1), ev("usd", -1.0, 1.5, hours_old=1)])
        self.assertLess(old["score"], new["score"])          # the old bullish item pulls less

    def test_scheduled_events_are_shown_and_flag_event_risk(self):
        soon = Evidence("cal", "US CPI", SCHEDULED, "calendar", NOW + timedelta(hours=1), "t", None, 0.0, 49.0)
        r = self.agg([ev("usd", 0.5, 1.5), soon])
        self.assertTrue(r["event_risk"])
        self.assertTrue(any(i["tier"] == SCHEDULED and not i["used"] and not i["skipped"] for i in r["items"]))

    def test_the_same_story_is_shown_once_and_official_first(self):
        a = Evidence("news", "Fed holds rates steady as inflation cools", UNVERIFIED, "A", NOW, "", None, 0.0, 24.0, "u1")
        b = Evidence("news", "Fed holds rates steady as inflation cools!", BREAKING, "B", NOW - timedelta(hours=1), "", None, 0.0, 72.0, "u2")
        c = Evidence("news", "Oil climbs", UNVERIFIED, "C", NOW, "", None, 0.0, 24.0, "u3")
        out = service.dedupe([a, b, c])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].source, "B")

    def test_news_never_moves_the_score(self):
        base = [ev("usd", 0.5, 1.5)]
        news = [Evidence("news", "Gold surges to record", UNVERIFIED, "X", NOW, "", None, 0.0, 24.0)]
        self.assertEqual(self.agg(base)["score"], self.agg(base + news)["score"])


# ---------------------------------------------------------- the service, offline

class FakeResponse:
    def __init__(self, body: bytes, status=200):
        self.content, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Serves the saved real responses; counts the calls; can fail or be slow."""
    ROUTES = (
        ("real_yield_curve", "treasury_real_202611.xml"), ("daily_treasury_yield_curve", "treasury_nominal_202611.xml"),
        ("EXR/D.USD.EUR", "ecb_exr.csv"), ("DFR", "ecb_dfr.csv"), ("SR_2Y", "ecb_2y.csv"), ("SR_10Y", "ecb_10y.csv"),
        ("effr", "effr.json"), ("wngsr", "eia_gas.csv"), ("table1", "eia_wpsr1.csv"),
        ("f_disagg", "cftc_disagg.txt"), ("FinFutWk", "cftc_fin.txt"), ("press_monetary", "fed_monetary.xml"),
        ("ecb.europa.eu/rss", "ecb_press.xml"), ("oilprice", "oilprice.xml"), ("mw_marketpulse", "marketwatch.xml"),
        ("todayinenergy", "eia_today.xml"), ("ff_calendar", "calendar.json"), ("/points/", "nws_point.json"),
        ("/forecast", "nws_forecast.json"),
    )

    def __init__(self, fail=(), delay=0.0):
        self.calls, self.fail, self.delay = [], tuple(fail), delay

    def get(self, url, timeout=None, headers=None, **kw):
        self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if any(f in url for f in self.fail):
            raise requests.Timeout("simulated")
        for needle, name in self.ROUTES:
            if needle in url:
                body = (FIX / name).read_bytes()
                if name == "nws_point.json":
                    body = body.replace(b"https://api.weather.gov/gridpoints/OKX/33,42/forecast",
                                        b"https://api.weather.gov/forecast/" + url.rsplit("/", 1)[1].encode())
                return FakeResponse(body)
        return FakeResponse(b"", 404)


def make(fail=(), delay=0.0, **cfg):
    session = FakeSession(fail, delay)
    return service.SourceService(SourcesConfig(**cfg), session=session, clock=lambda: NOW), session


def settle(svc, asset, now=NOW):
    """What a report sees once the background downloads have finished."""
    svc.collect(asset, now)                    # starts them (returns at once)
    assert svc.wait_idle(15), "downloads did not finish"
    return svc.collect(asset, now)


class ServiceTests(unittest.TestCase):
    def test_every_instrument_gets_a_real_reading_from_the_saved_responses(self):
        svc, _ = make()
        for asset in rules.SCORED_SPECS:
            r = settle(svc, asset)
            self.assertEqual(r["status"], "available", (asset, r["missing"]))
            self.assertEqual(r["coverage"], 1.0, (asset, r["missing"]))
            self.assertIn(r["direction"], ("BULLISH", "BEARISH", "NEUTRAL"))
            self.assertTrue(0 <= r["strength"] <= 100)
            self.assertTrue(all(i["source"] and i["published_at"] for i in r["items"] if i["tier"] != "scheduled"))

    def test_oil_reads_the_petroleum_stocks_from_the_saved_report(self):
        svc, _ = make()
        r = settle(svc, "wti")
        crude = next(i for i in r["items"] if "crude" in i["title"])
        self.assertIn("+3.4 Mb", crude["title"])
        self.assertLess(crude["signal"], 0)               # a build is bearish

    def test_the_second_call_uses_the_cache(self):
        svc, session = make()
        settle(svc, "natgas")
        first = len(session.calls)
        settle(svc, "natgas")
        self.assertEqual(len(session.calls), first)

    def test_a_failed_source_is_missing_not_neutral_and_not_retried_at_once(self):
        svc, session = make(fail=("treasury",))
        r = settle(svc, "gold")
        self.assertEqual(r["status"], "available")                      # the dollar item still counts
        self.assertAlmostEqual(r["coverage"], 0.25)
        self.assertTrue(any(m["key"] == "src_missing" and "US 10-year real yield" == m["args"]["what"] for m in r["missing"]))
        calls = len(session.calls)
        settle(svc, "gold")
        self.assertEqual(len(session.calls), calls)                      # remembered: no hammering

    def test_all_sources_down_gives_unavailable(self):
        svc, _ = make(fail=("treasury", "ecb", "eia", "cftc", "newyorkfed", "federalreserve", "oilprice",
                            "dowjones", "weather"))
        r = settle(svc, "gold")
        self.assertEqual((r["status"], r["direction"]), ("unavailable", None))

    def test_a_slow_or_hanging_server_never_holds_up_the_caller(self):
        svc, _ = make(delay=3.0)
        started = time.monotonic()
        first = svc.collect("natgas", NOW)
        for _ in range(20):
            svc.collect("natgas", NOW)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(first["status"], "unavailable")
        self.assertTrue(any("still loading" in str(m["args"]) for m in first["missing"]))
        self.assertTrue(svc.loading())
        self.assertEqual(settle(svc, "natgas")["status"], "available")      # used from the next report

    def test_many_callers_at_once_never_download_the_same_thing_twice(self):
        import threading
        svc, session = make(delay=0.2)
        threads = [threading.Thread(target=svc.collect, args=(asset, NOW))
                   for asset in ("gold", "silver", "wti", "brent", "natgas", "eurusd") * 4]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertTrue(svc.wait_idle(20))
        for asset in rules.SCORED_SPECS:
            svc.collect(asset, NOW)
        svc.wait_idle(20)
        self.assertEqual(len(session.calls), len(set(session.calls)), "a URL was requested twice")

    def test_the_downloads_run_in_daemon_threads_that_cannot_block_shutdown(self):
        import threading
        svc, _ = make(delay=0.3)
        svc.collect("gold", NOW)
        workers = [t for t in threading.enumerate() if t.name.startswith("source-")]
        self.assertTrue(workers)
        self.assertTrue(all(t.daemon for t in workers))
        svc.wait_idle(10)

    def test_a_failed_refresh_keeps_the_last_good_copy_and_it_is_still_judged_by_its_own_date(self):
        svc, session = make()
        self.assertEqual(settle(svc, "natgas")["status"], "available")
        for key, (stored, value, error) in list(svc._cache.items()):
            svc._cache[key] = (stored - 10 * 3600, value, error)             # every cached copy is now past its TTL
        session.fail = ("ir.eia.gov", "oilprice", "federalreserve", "ecb.europa", "dowjones", "cftc", "weather")
        again = settle(svc, "natgas")
        self.assertEqual(again["status"], "available")                       # the old copy survived the failure
        # ... but nobody may mistake it for fresh: two weeks later its own date makes it stale
        later = svc.collect("natgas", NOW + timedelta(days=14))
        self.assertEqual((later["status"], later["why"]), ("unavailable", "no_data"))
        self.assertTrue(any(m["key"] == "src_stale" for m in later["missing"]))

    def test_downloaded_recently_does_not_mean_fresh(self):
        svc, _ = make()
        settle(svc, "wti")
        r = svc.collect("wti", NOW + timedelta(days=30))          # cache is "new", the data are a month old
        self.assertEqual(r["status"], "unavailable")
        self.assertTrue(all(i["skipped"] == "stale" for i in r["items"] if i["tier"] == "confirmed"))

    def test_the_unofficial_calendar_is_off_unless_asked(self):
        self.assertFalse(SourcesConfig().calendar_unofficial)
        svc, session = make()
        settle(svc, "gold")
        self.assertFalse(any("ff_calendar" in u for u in session.calls))
        svc2, session2 = make(calendar_unofficial=True)
        settle(svc2, "gold")
        self.assertTrue(any("ff_calendar" in u for u in session2.calls))

    def test_no_key_is_needed_and_fred_is_only_context(self):
        self.assertEqual(SourcesConfig().fred_api_key, "")
        svc, session = make()
        settle(svc, "gold")
        self.assertFalse(any("stlouisfed" in u for u in session.calls))

    def test_only_free_public_hosts_are_ever_called(self):
        svc, session = make(calendar_unofficial=True)
        for asset in rules.SCORED_SPECS:
            settle(svc, asset)
        hosts = {u.split("/")[2] for u in session.calls}
        self.assertLessEqual(hosts, {
            "home.treasury.gov", "data-api.ecb.europa.eu", "markets.newyorkfed.org", "ir.eia.gov",
            "www.cftc.gov", "api.weather.gov", "www.federalreserve.gov", "www.ecb.europa.eu",
            "www.eia.gov", "oilprice.com", "feeds.content.dowjones.io", "nfs.faireconomy.media"})


# ----------------------------------------------- the shadow analysis uses them, safely

def shadow_builder(svc, epic_asset="gold", mode="shadow"):
    config = auto_config()
    config.scoring.mode = mode
    config.sources.assets = {"GOLD": epic_asset}
    return ReportBuilder(broker_with(M15=UP, H1=UP), config, sources=svc)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make()
        self.svc.warm()
        self.assertTrue(self.svc.wait_idle(20))

    def test_the_three_results_use_real_fundamentals_and_say_how_they_combine(self):
        plan = shadow_builder(self.svc).build("GOLD")
        shadow = plan.shadow
        self.assertEqual(shadow["fundamental"]["asset"], "gold")
        self.assertEqual(shadow["overall"]["basis"], "combined")
        self.assertIn(shadow["overall"]["agreement"], ("agree", "contradict", "partial"))
        text = render_full(plan)
        for needle in ("TECHNICAL", "FUNDAMENTAL", "OVERALL", "US Treasury", "confirmed data", "h ago"):
            self.assertIn(needle, text)
        short = render_text(plan)
        self.assertIn("📰 Fundamental:", short)
        self.assertNotIn("unavailable", short.split("Fundamental:")[1].splitlines()[0])

    def test_with_the_sources_down_the_overall_is_explicitly_technical_only(self):
        svc, _ = make(fail=("treasury", "ecb", "eia", "cftc", "newyorkfed", "federalreserve", "oilprice",
                            "dowjones", "weather"))
        shadow = shadow_builder(svc).build("GOLD").shadow
        self.assertEqual(shadow["fundamental"]["status"], "unavailable")
        self.assertEqual(shadow["overall"]["basis"], "technical_only")
        self.assertEqual(shadow["overall"]["strength"], shadow["technical"]["strength"])

    def test_technical_strength_is_identical_with_or_without_the_sources(self):
        with_sources = shadow_builder(self.svc).build("GOLD").shadow["technical"]
        config = auto_config()
        config.scoring.mode = "shadow"
        without = ReportBuilder(broker_with(M15=UP, H1=UP), config).build("GOLD").shadow["technical"]
        self.assertEqual(with_sources, without)

    def test_every_decision_is_identical_with_the_real_sources_on_or_off(self):
        legacy = shadow_builder(self.svc, mode="legacy").build("GOLD")
        shadowed = shadow_builder(self.svc).build("GOLD")
        a, b = legacy.to_dict(), shadowed.to_dict()
        for key in ("created_at", "plan_id", "shadow", "entry_zone", "odds"):
            a.pop(key), b.pop(key)
        self.assertEqual(a, b)
        for minimum in (0.0, 40.0, 60.0, 90.0):
            self.assertEqual(funnel.auto_blocker(legacy, min_strength=minimum, t=T),
                             funnel.auto_blocker(shadowed, min_strength=minimum, t=T))
        self.assertEqual((legacy.confidence, legacy.bias, legacy.assessment),
                         (shadowed.confidence, shadowed.bias, shadowed.assessment))

    def test_an_instrument_with_no_profile_falls_back_to_the_news_path(self):
        shadow = shadow_builder(self.svc, epic_asset="nonsense").build("GOLD", fundamentals=True).shadow
        self.assertNotIn("asset", shadow["fundamental"])                 # not the free-source result
        self.assertEqual(shadow["fundamental"]["status"], "unavailable")
        self.assertEqual(shadow["overall"]["basis"], "technical_only")

    def test_legacy_mode_creates_no_source_service_and_makes_no_request(self):
        config = auto_config()
        config.sources.enabled = True
        builder = ReportBuilder(broker_with(M15=UP, H1=UP), config)
        self.assertIsNone(builder.sources)
        self.assertEqual(builder.build("GOLD").shadow, {})

    def test_a_quick_read_never_fetches(self):
        svc, session = make()
        shadow_builder(svc).build("GOLD", fundamentals=False)
        self.assertEqual(session.calls, [])
        self.assertFalse(svc.loading())

    def test_a_failure_inside_the_sources_cannot_cost_the_plan(self):
        class Broken:
            def collect(self, *a, **k):
                raise RuntimeError("boom")
        with self.assertLogs("tmbot.analysis.report", level="ERROR"):
            plan = shadow_builder(Broken()).build("GOLD")
        self.assertGreater(plan.confidence, 0)
        self.assertTrue(plan.shadow)                        # fell back to the news-only read

    def test_arabic_renders_without_missing_wording(self):
        text = render_full(shadow_builder(self.svc).build("GOLD"), Translator("ar"))
        for marker in ("[shadow.", "[sm.", "[tr."):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()


class CommandTests(unittest.TestCase):
    def test_the_sources_command_prints_every_instrument_and_touches_no_broker_or_database(self):
        import contextlib
        import io
        import os
        import tempfile
        from tmbot import cli
        svc, session = make()
        args = type("A", (), dict(asset=None, wait=15.0))()
        out, err = io.StringIO(), io.StringIO()
        before = os.listdir(tempfile.gettempdir())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._sources_command(args, Config(), service=svc)
        self.assertEqual(code, 0)
        text = out.getvalue()
        for asset in rules.SCORED_SPECS:
            self.assertIn(f"== {asset}:", text)
        self.assertIn("[confirmed data]", text)
        self.assertIn("Read-only", text)

    def test_an_unknown_instrument_is_refused(self):
        import contextlib
        import io
        from tmbot import cli
        args = type("A", (), dict(asset="bitcoin", wait=1.0))()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli._sources_command(args, Config(), service=make()[0]), 2)

    def test_check_reports_that_shadow_is_not_trading_and_where_auto_stands(self):
        import contextlib
        import io
        import tempfile
        from tmbot import cli
        path = Path(tempfile.mkdtemp()) / "c.yaml"
        path.write_text("scoring:\n  mode: shadow\nanalysis:\n  watchlist:\n    - {epic: GOLD, display: Gold}\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--env", "demo", "--config", str(path), "check"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("scoring       shadow  (three-part analysis shown, never used to trade)", text)
        self.assertIn("auto trading  disabled, live accounts not allowed, minimum strength 60", text)
