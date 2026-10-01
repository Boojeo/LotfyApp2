"""Entry quality and timeframe alignment, kept apart from direction."""

from __future__ import annotations

import unittest

from tmbot.analysis import quality
from tmbot.models import Bias, Direction, Level


def judge(**overrides):
    args = dict(
        bias=Bias.BEARISH, direction=Direction.SELL, price=3400.0, atr=4.0,
        ema_fast=3401.0, rsi=45.0, stop=3406.0, tp1=3392.0, levels=[],
        spread=0.2, tp1_pushed=False, higher_timeframe="H1",
        higher_bias=Bias.BEARISH, risk_verdict="OK",
    )
    args.update(overrides)
    return quality.assess(**args)


def keys(result):
    return [flag.key for flag in result.flags]


class DirectionIsNotEntryTests(unittest.TestCase):
    def test_a_clean_setup_is_good_and_approved(self):
        result = judge()
        self.assertEqual((result.quality, result.verdict), ("GOOD", "APPROVED"))
        self.assertEqual(result.alignment, "ALIGNED")

    def test_bearish_but_stretched_into_support_is_weak_not_a_free_short(self):
        result = judge(ema_fast=3409.0, levels=[Level(price=3399.0, kind="support")])
        self.assertEqual(result.quality, "WEAK")
        self.assertEqual(result.verdict, "CAUTION")
        self.assertIn("extended", keys(result))
        self.assertIn("near_level", keys(result))

    def test_stretched_rsi_counts_against_the_entry(self):
        self.assertIn("rsi_stretched", keys(judge(rsi=25.0)))
        self.assertNotIn("rsi_stretched", keys(judge(rsi=25.0, bias=Bias.BULLISH,
                                                     direction=Direction.BUY,
                                                     higher_bias=Bias.BULLISH,
                                                     stop=3394.0, tp1=3408.0)))

    def test_three_small_problems_make_a_poor_entry(self):
        result = judge(ema_fast=3409.0, rsi=25.0, tp1_pushed=True)
        self.assertEqual((result.quality, result.verdict), ("POOR", "REJECTED"))

    def test_no_direction_means_no_entry(self):
        result = judge(bias=Bias.NEUTRAL)
        self.assertEqual((result.quality, result.verdict), ("NONE", "REJECTED"))

    def test_levels_built_against_the_bias_are_poor(self):
        result = judge(bias=Bias.BULLISH)
        self.assertIn("against_bias", keys(result))
        self.assertEqual(result.quality, "POOR")


class TimeframeTests(unittest.TestCase):
    def test_a_higher_timeframe_pointing_the_other_way_is_a_conflict(self):
        result = judge(higher_bias=Bias.BULLISH)
        self.assertEqual(result.alignment, "CONFLICT")
        self.assertEqual(result.quality, "POOR", "a conflict lowers quality, it is not averaged")

    def test_a_directionless_higher_timeframe_is_partial(self):
        result = judge(higher_bias=Bias.NEUTRAL)
        self.assertEqual((result.alignment, result.quality), ("PARTIAL", "WEAK"))

    def test_missing_higher_data_is_uncertain_not_aligned(self):
        self.assertEqual(judge(higher_bias=None).alignment, "UNCERTAIN")


class RiskVerdictTests(unittest.TestCase):
    def test_the_risk_limit_overrides_a_perfect_setup(self):
        result = judge(risk_verdict="REJECTED")
        self.assertEqual((result.verdict, result.verdict_key),
                         ("REJECTED", "verdict.risk_limit"))

    def test_an_unchecked_risk_is_never_approved(self):
        self.assertEqual(judge(risk_verdict=None).verdict, "CAUTION")
        self.assertEqual(judge(risk_verdict="UNCHECKED").verdict, "CAUTION")


class DisplayTests(unittest.TestCase):
    def test_the_report_shows_verdict_quality_and_alignment_separately(self):
        from tmbot.analysis.report import ReportBuilder, render_text
        from tests.test_style import UP, auto_config, broker_with
        plan = ReportBuilder(broker_with(M15=UP, H1=UP), auto_config()).build("GOLD")
        text = render_text(plan)
        self.assertIn("signal strength", text)
        self.assertNotIn("confidence", text)
        self.assertIn("VERDICT:", text)
        self.assertIn("Entry quality:", text)
        self.assertIn("Timeframes:", text)


if __name__ == "__main__":
    unittest.main()
