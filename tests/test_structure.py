"""Price gaps, Fibonacci, Wolfe waves and Camarilla pivots."""

from __future__ import annotations

import unittest
from datetime import timedelta

from tmbot.analysis import structure
from tmbot.i18n import CATALOG, Translator
from tmbot.models import Candle, TradePlan
from tests.helpers import BASE


def bar(index, close, *, high=None, low=None, open_=None):
    return Candle(ts=BASE + timedelta(minutes=15 * index),
                  open=close if open_ is None else open_,
                  high=close + 0.1 if high is None else high,
                  low=close - 0.1 if low is None else low, close=close)


def path(vertices):
    """Straight-line closes through (index, price) vertices."""
    out = []
    for (i0, p0), (i1, p1) in zip(vertices, vertices[1:]):
        for index in range(i0, i1):
            out.append(bar(len(out), p0 + (p1 - p0) * (index - i0) / (i1 - i0)))
    out.append(bar(len(out), vertices[-1][1]))
    return out


def range_then_gap(*, after=None):
    """25 quiet bars in 99-101, then a bar, a big impulse bar and a third bar
    that leave a bullish gap between 101 and 103."""
    bars = [bar(i, 100, high=101, low=99) for i in range(25)]
    bars.append(bar(25, 100.5, high=101, low=100))
    bars.append(bar(26, 105.5, high=106, low=100.9))
    bars.append(bar(27, 104, high=105, low=103))
    for extra in after or []:
        bars.append(extra)
    return bars


class GapTests(unittest.TestCase):
    def test_a_bullish_gap_leaving_a_tight_range_is_a_breakaway_gap(self):
        gaps = structure.fair_value_gaps(range_then_gap(), 104.0, 1.0)
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual((gap["kind"], gap["type"], gap["state"]),
                         ("bullish", "breakaway", "open"))
        self.assertEqual((gap["low"], gap["high"]), (101, 103))

    def test_price_trading_into_the_gap_fills_it_partly_then_fully(self):
        partly = range_then_gap(after=[bar(28, 102.5, high=104, low=102)])
        gap = structure.fair_value_gaps(partly, 102.5, 1.0)[0]
        self.assertEqual(gap["state"], "partial")
        self.assertAlmostEqual(gap["filled"], 0.5)
        filled = range_then_gap(after=[bar(28, 100.5, high=104, low=100.5)])
        self.assertEqual(structure.fair_value_gaps(filled, 100.5, 1.0), [])

    def test_a_small_gap_is_common_and_a_tiny_one_is_ignored(self):
        small = [bar(i, 100, high=101, low=99) for i in range(25)]
        small += [bar(25, 100, high=100.2, low=99.9), bar(26, 100.4, high=100.6, low=100.3),
                  bar(27, 100.6, high=100.8, low=100.45)]
        gap = structure.fair_value_gaps(small, 100.6, 1.0)[0]
        self.assertEqual(gap["type"], "common")
        tiny = small[:-1] + [bar(27, 100.6, high=100.8, low=100.28)]
        self.assertEqual(structure.fair_value_gaps(tiny, 100.6, 1.0), [])

    def test_a_bearish_gap_is_found_too(self):
        bars = [bar(i, 100, high=101, low=99) for i in range(25)]
        bars += [bar(25, 99.5, high=100, low=99), bar(26, 94.5, high=98.5, low=94),
                 bar(27, 96, high=97, low=95)]
        gap = structure.fair_value_gaps(bars, 96.0, 1.0)[0]
        self.assertEqual((gap["kind"], gap["low"], gap["high"]), ("bearish", 97, 99))


class FibonacciTests(unittest.TestCase):
    def leg(self, last):
        return path([(0, 105), (5, 100), (15, 110), (20, last), (24, last)])

    def test_the_golden_pocket_is_named_and_levels_split_around_price(self):
        fib = structure.fibonacci(self.leg(103.5), 103.5, 1.0)
        self.assertEqual(fib["direction"], "up")
        self.assertEqual(fib["zone"], "golden")
        self.assertAlmostEqual(fib["start"], 99.9)
        self.assertAlmostEqual(fib["end"], 110.1)
        roles = {level["role"] for level in fib["levels"]}
        self.assertEqual(roles, {"support", "resistance"})
        half = next(l for l in fib["levels"] if l["ratio"] == 0.5)
        self.assertAlmostEqual(half["price"], 105.0)

    def test_a_shallow_pullback_and_a_broken_leg(self):
        self.assertEqual(structure.fibonacci(self.leg(108), 108, 1.0)["zone"], "shallow")
        # still falling through the start of the leg: the new low is unconfirmed
        falling = path([(0, 105), (5, 100), (15, 110), (22, 98)])
        self.assertEqual(structure.fibonacci(falling, 98, 1.0)["zone"], "broken")

    def test_a_leg_smaller_than_the_threshold_is_ignored(self):
        self.assertIsNone(structure.fibonacci(self.leg(103.5), 103.5, 10.0))


class WolfeTests(unittest.TestCase):
    def wedge(self, bullish=True):
        vertices = [(0, 104), (10, 100), (20, 110), (30, 95), (40, 102), (50, 86), (55, 89)]
        if not bullish:
            vertices = [(i, 200 - p) for i, p in vertices]
        return path(vertices)

    def test_a_bullish_wedge_gives_an_entry_zone_and_a_target(self):
        wolfe = structure.wolfe_wave(self.wedge(), 89.0)
        self.assertEqual(wolfe["direction"], "bullish")
        self.assertAlmostEqual(wolfe["entry"], 85.9)
        self.assertGreater(wolfe["target"], 89.0)

    def test_a_bearish_wedge_is_the_mirror_image(self):
        wolfe = structure.wolfe_wave(self.wedge(False), 111.0)
        self.assertEqual(wolfe["direction"], "bearish")
        self.assertLess(wolfe["target"], 111.0)

    def test_a_trend_with_no_wedge_gives_nothing(self):
        self.assertIsNone(structure.wolfe_wave(path([(0, 100), (60, 130)]), 130.0))


class CamarillaTests(unittest.TestCase):
    def daily(self):
        return [Candle(ts=BASE, open=104, high=110, low=100, close=105),
                bar(1, 105)]

    def test_levels_come_from_the_previous_session(self):
        cam = structure.camarilla(self.daily(), 105.0)
        self.assertAlmostEqual(cam["r3"], 107.75)
        self.assertAlmostEqual(cam["s3"], 102.25)
        self.assertAlmostEqual(cam["r4"], 110.5)
        self.assertAlmostEqual(cam["pivot"], 105.0)
        self.assertEqual(cam["zone"], "range")

    def test_where_price_sits_is_named(self):
        zones = {price: structure.camarilla(self.daily(), price)["zone"]
                 for price in (111, 108, 102, 99)}
        self.assertEqual(zones, {111: "above_r4", 108: "above_r3",
                                 102: "below_s3", 99: "below_s4"})

    def test_one_day_of_history_is_not_enough(self):
        self.assertIsNone(structure.camarilla(self.daily()[:1], 105.0))


class DynamicTests(unittest.TestCase):
    def test_moving_averages_support_a_rising_price(self):
        levels = structure.dynamic_levels(path([(0, 100), (260, 160)]), 160.0)
        self.assertEqual([lv["name"] for lv in levels], ["EMA20", "EMA50", "EMA200"])
        self.assertTrue(all(lv["role"] == "support" and lv["rising"] for lv in levels))


class DisplayTests(unittest.TestCase):
    def market(self, zone="golden", gap="breakaway", cam="range"):
        return {
            "gaps": [{"kind": "bullish", "type": gap, "low": 101.0, "high": 103.0,
                      "size_atr": 2.0, "state": "partial", "filled": 0.5, "bars_ago": 3}],
            "fibonacci": structure.fibonacci(
                path([(0, 105), (5, 100), (15, 110), (20, 103.5), (24, 103.5)]), 103.5, 1.0)
            | {"zone": zone},
            "static": {"support": {"price": 100.0, "touches": 3, "liquidity": False},
                       "resistance": None},
            "dynamic": [{"name": "EMA20", "price": 102.0, "role": "support", "rising": True}],
            "wolfe": {"direction": "bullish", "entry": 99.0, "target": 108.0,
                      "points": [], "bars_ago": 2},
            "camarilla": structure.camarilla(
                [Candle(ts=BASE, open=104, high=110, low=100, close=105), bar(1, 105)], 105.0)
            | {"zone": cam},
        }

    def plan(self, market):
        from tests.test_style import UP, auto_config, broker_with
        from tmbot.analysis.report import ReportBuilder
        plan = ReportBuilder(broker_with(M15=UP, H1=UP), auto_config()).build("GOLD")
        plan.market = market
        return plan

    def test_every_reading_has_wording_in_both_languages(self):
        from tmbot.analysis.report import render_full
        for zone in ("shallow", "moderate", "deep", "golden", "very_deep", "broken",
                     "extension"):
            for gap in ("breakaway", "continuation", "exhaustion", "common"):
                for cam in ("range", "above_r3", "above_r4", "below_s3", "below_s4"):
                    plan = self.plan(self.market(zone, gap, cam))
                    for language in ("en", "ar"):
                        text = render_full(plan, Translator(language))
                        self.assertNotIn("[mm.", text)
                        self.assertNotIn("[term.", text)
                        self.assertIn("🧭", text)

    def test_the_section_names_every_family_the_plan_was_asked_for(self):
        from tmbot.analysis.report import render_full
        text = render_full(self.plan(self.market()))
        for label in ("Fair Value Gap", "breakaway", "Fibonacci (up leg",
                      "golden pocket", "Fibonacci Support/Resistance",
                      "Static Support/Resistance", "Dynamic (Moving)",
                      "Wolfe Wave", "Camarilla Pivots"):
            self.assertIn(label, text)

    def test_the_map_is_saved_with_the_plan_and_old_plans_still_load(self):
        plan = self.plan(self.market())
        self.assertEqual(TradePlan.from_dict(plan.to_dict()).market, plan.market)
        raw = plan.to_dict()
        raw.pop("market")
        old = TradePlan.from_dict(raw)
        self.assertEqual(old.market, {})
        from tmbot.analysis.report import render_full
        self.assertNotIn("Market Map", render_full(old))

    def test_the_gap_terms_exist_in_the_catalogue(self):
        for key in ("mm.heading", "mm.wolfe_none", "mm.gap_none", "mm.fib.extension"):
            self.assertTrue(CATALOG[key].get("ar"))


if __name__ == "__main__":
    unittest.main()
