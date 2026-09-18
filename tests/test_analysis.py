"""Indicators, level mapping and the report assembly."""

from __future__ import annotations

import unittest

from tmbot.analysis.bias import analyse
from tmbot.analysis.indicators import adx, atr, ema, last_value, rsi, sma, swing_points
from tmbot.analysis.levels import build_plan, find_zones
from tmbot.config import AnalysisConfig
from tmbot.models import Bias, Direction
from tests.helpers import candles


class IndicatorTests(unittest.TestCase):
    def test_series_are_padded_to_the_input_length(self):
        values = [float(i) for i in range(50)]
        for series in (sma(values, 10), ema(values, 10), rsi(values, 14)):
            self.assertEqual(len(series), len(values))

    def test_sma_of_a_ramp_is_the_window_midpoint(self):
        values = [float(i) for i in range(10)]
        self.assertAlmostEqual(sma(values, 5)[-1], 7.0)

    def test_atr_of_constant_range_candles_equals_that_range(self):
        bars = candles(60, drift=0.0, wave=0.0, spread=2.0)
        # Every bar spans high-low = 4.0 with no gaps.
        self.assertAlmostEqual(last_value(atr(bars, 14)), 4.0, places=6)

    def test_rsi_is_pinned_high_in_an_unbroken_advance(self):
        values = [100.0 + i for i in range(40)]
        self.assertAlmostEqual(last_value(rsi(values, 14)), 100.0, places=6)

    def test_adx_rises_in_a_trend_and_stays_low_in_a_range(self):
        trending = last_value(adx(candles(200, drift=2.0), 14)[0])
        ranging = last_value(adx(candles(200, drift=0.0, wave=6.0), 14)[0])
        self.assertGreater(trending, ranging)

    def test_swing_points_need_confirmation_bars_on_both_sides(self):
        bars = candles(60, drift=0.0, wave=8.0)
        highs, lows = swing_points(bars, left=2, right=2)
        self.assertTrue(highs and lows)
        # Nothing inside the final `right` bars can be confirmed yet.
        self.assertLessEqual(max(index for index, _ in highs), len(bars) - 3)


class LevelTests(unittest.TestCase):
    def setUp(self):
        self.config = AnalysisConfig()
        self.bars = candles(300, drift=0.4, wave=6.0)
        self.price = self.bars[-1].close

    def test_a_plan_always_has_three_targets_and_one_stop(self):
        for direction in (Direction.BUY, Direction.SELL):
            plan = build_plan(self.bars, self.price, direction, self.config)
            self.assertEqual(len(plan.targets), 3)
            self.assertTrue(all(isinstance(target, float) for target in plan.targets))
            self.assertIsInstance(plan.sl, float)

    def test_targets_run_away_from_price_in_the_trade_direction(self):
        plan = build_plan(self.bars, self.price, Direction.BUY, self.config)
        self.assertLess(self.price, plan.tp1)
        self.assertLess(plan.tp1, plan.tp2)
        self.assertLess(plan.tp2, plan.tp3)
        self.assertLess(plan.sl, self.price)

        short = build_plan(self.bars, self.price, Direction.SELL, self.config)
        self.assertGreater(self.price, short.tp1)
        self.assertGreater(short.tp1, short.tp2)
        self.assertGreater(short.tp2, short.tp3)
        self.assertGreater(short.sl, self.price)

    def test_tp1_always_pays_at_least_the_configured_reward_ratio(self):
        plan = build_plan(self.bars, self.price, Direction.BUY, self.config)
        risk = abs(self.price - plan.sl)
        self.assertGreaterEqual(abs(plan.tp1 - self.price) / risk,
                                self.config.min_reward_risk - 1e-9)

    def test_a_flat_series_still_produces_a_usable_plan(self):
        flat = candles(120, drift=0.0, wave=0.0, spread=0.0)
        plan = build_plan(flat, flat[-1].close, Direction.BUY, self.config)
        self.assertEqual(len(plan.targets), 3)
        self.assertLess(plan.sl, flat[-1].close)
        self.assertEqual(plan.method, "atr-projection")

    def test_repeated_touches_of_one_level_are_tagged_as_liquidity(self):
        bars = candles(300, drift=0.0, wave=8.0)
        zones = find_zones(bars, 4.0, self.config)
        self.assertTrue(any(zone.is_liquidity for zone in zones))


class BiasTests(unittest.TestCase):
    def test_a_steady_advance_reads_bullish(self):
        read = analyse(candles(300, drift=1.2, wave=1.0), AnalysisConfig())
        self.assertIs(read.bias, Bias.BULLISH)
        self.assertGreater(read.confidence, 50)

    def test_a_steady_decline_reads_bearish(self):
        read = analyse(candles(300, drift=-1.2, wave=1.0), AnalysisConfig())
        self.assertIs(read.bias, Bias.BEARISH)

    def test_a_deep_pullback_inside_an_uptrend_reads_neutral(self):
        # Long-term structure is up but every short-term factor is against it.
        # Calling that BULLISH is how you buy the top of a retracement.
        read = analyse(candles(300, drift=1.2, wave=4.0), AnalysisConfig())
        self.assertIs(read.bias, Bias.NEUTRAL)
        self.assertGreater(read.factors[0].value, 0)   # trend structure still up
        self.assertLess(read.factors[6].value, 0)      # but price is at the low of the range

    def test_every_factor_is_reported_with_its_weight(self):
        read = analyse(candles(300, drift=0.5), AnalysisConfig())
        self.assertGreaterEqual(len(read.factors), 8)
        for factor in read.factors:
            self.assertGreaterEqual(factor.value, -1.0)
            self.assertLessEqual(factor.value, 1.0)
            self.assertGreater(factor.weight, 0)

    def test_too_little_history_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            analyse(candles(20), AnalysisConfig())


if __name__ == "__main__":
    unittest.main()
