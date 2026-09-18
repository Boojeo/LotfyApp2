"""The trade-management ladder, break-even and trailing logic."""

from __future__ import annotations

import unittest

from tmbot.config import LadderStep
from tmbot.manage.rules import DecisionKind, chandelier_stop, evaluate
from tmbot.models import Direction, MarketRules, TrendStrength
from tests.helpers import RULES, management, snapshot, trade


def kinds(evaluation):
    return [decision.kind for decision in evaluation.decisions]


def first(evaluation, kind):
    return next(d for d in evaluation.decisions if d.kind is kind)


class LadderTests(unittest.TestCase):
    def test_nothing_happens_before_tp1(self):
        result = evaluate(trade(), snapshot(3405.0), management())
        self.assertEqual(result.decisions, [])

    def test_tp1_closes_half_and_moves_stop_to_entry(self):
        result = evaluate(trade(), snapshot(3410.0), management())
        self.assertIn(DecisionKind.PARTIAL_CLOSE, kinds(result))
        partial = first(result, DecisionKind.PARTIAL_CLOSE)
        self.assertEqual(partial.size, 0.5)
        self.assertEqual(partial.stage.value, "TP1")

        stop = first(result, DecisionKind.SET_STOP)
        self.assertEqual(stop.stop_level, 3400.0, "stop must sit exactly on the entry price")

    def test_tp2_closes_a_quarter_of_the_original_size(self):
        managed = trade(size=2.0, tp1_done=True, breakeven_done=True, remaining_size=1.0,
                        stop_level=3400.0)
        result = evaluate(managed, snapshot(3420.0), management())
        partial = first(result, DecisionKind.PARTIAL_CLOSE)
        self.assertEqual(partial.size, 0.25 * managed.initial_size)
        self.assertEqual(partial.stage.value, "TP2")

    def test_partial_size_rounds_down_to_a_legal_increment(self):
        # 25% of 1.0 lot is 0.25, but the instrument only trades in 0.1 steps:
        # round down so the runner is never over-closed.
        managed = trade(size=1.0, tp1_done=True, breakeven_done=True, remaining_size=0.5,
                        stop_level=3400.0)
        result = evaluate(managed, snapshot(3420.0), management())
        self.assertEqual(first(result, DecisionKind.PARTIAL_CLOSE).size, 0.2)

    def test_completed_rungs_never_fire_again(self):
        managed = trade(tp1_done=True, tp2_done=True, breakeven_done=True,
                        remaining_size=0.25, stop_level=3400.0)
        result = evaluate(managed, snapshot(3421.0), management())
        self.assertNotIn(DecisionKind.PARTIAL_CLOSE, kinds(result))

    def test_a_gap_through_both_rungs_fires_them_in_order(self):
        result = evaluate(trade(size=2.0), snapshot(3421.0), management(extend_tp3=False))
        partials = [d for d in result.decisions if d.kind is DecisionKind.PARTIAL_CLOSE]
        self.assertEqual([p.stage.value for p in partials], ["TP1", "TP2"])
        self.assertEqual([p.size for p in partials], [1.0, 0.5])

    def test_break_even_fires_even_when_the_partial_cannot(self):
        # A size too small to split still has to be de-risked.
        managed = trade(size=0.1)
        result = evaluate(managed, snapshot(3410.0), management())
        self.assertNotIn(DecisionKind.PARTIAL_CLOSE, kinds(result))
        self.assertEqual(first(result, DecisionKind.SET_STOP).stop_level, 3400.0)
        self.assertTrue(any("minimum deal size" in note.reason for note in result.blocked))

    def test_an_indivisible_remainder_is_held_by_default(self):
        # An 80% rung on a 1.0 lot would leave 0.2 -- below this instrument's
        # 0.3 minimum -- so the partial is skipped and the full size rides on.
        chunky = MarketRules(epic="GOLD", min_deal_size=0.3, size_step=0.1, decimal_places=2)
        config = management(ladder=[LadderStep("TP1", 0.8)])
        result = evaluate(trade(), snapshot(3410.0, rules=chunky), config)
        self.assertNotIn(DecisionKind.PARTIAL_CLOSE, kinds(result))
        self.assertTrue(any("below the" in note.reason for note in result.blocked))

    def test_indivisible_remainder_can_close_everything_instead(self):
        chunky = MarketRules(epic="GOLD", min_deal_size=0.3, size_step=0.1, decimal_places=2)
        config = management(ladder=[LadderStep("TP1", 0.8)], on_indivisible_size="close_all")
        result = evaluate(trade(), snapshot(3410.0, rules=chunky), config)
        self.assertEqual(first(result, DecisionKind.CLOSE_ALL).size, 1.0)

    def test_tp3_closes_the_runner(self):
        managed = trade(tp1_done=True, tp2_done=True, breakeven_done=True,
                        remaining_size=0.25, stop_level=3400.0)
        result = evaluate(managed, snapshot(3431.0), management(extend_tp3=False))
        close = first(result, DecisionKind.CLOSE_ALL)
        self.assertEqual(close.size, 0.25)


class ShortSideTests(unittest.TestCase):
    def test_short_ladder_mirrors_the_long_one(self):
        managed = trade(Direction.SELL)          # entry 3400, TP1 3390, SL 3410
        result = evaluate(managed, snapshot(3390.0), management())
        partial = first(result, DecisionKind.PARTIAL_CLOSE)
        self.assertEqual(partial.size, 0.5)
        self.assertEqual(first(result, DecisionKind.SET_STOP).stop_level, 3400.0)

    def test_short_trailing_stop_only_ever_falls(self):
        managed = trade(Direction.SELL, tp1_done=True, breakeven_done=True,
                        remaining_size=0.5, stop_level=3395.0, best_price=3370.0)
        result = evaluate(
            managed,
            snapshot(3372.0, atr=4.0, strength=TrendStrength.STRONG, swing_high=3384.0),
            management(),
        )
        stop = first(result, DecisionKind.SET_STOP)
        self.assertLess(stop.stop_level, 3395.0)
        self.assertGreater(stop.stop_level, 3372.0)


class TrailingTests(unittest.TestCase):
    def test_no_trail_until_the_configured_stage(self):
        result = evaluate(trade(), snapshot(3405.0), management(trail_after_stage="TP1"))
        self.assertEqual(result.decisions, [])

    def test_chandelier_uses_the_strong_trend_multiple(self):
        managed = trade(tp1_done=True, breakeven_done=True, remaining_size=0.5,
                        stop_level=3400.0, best_price=3438.2)
        snap = snapshot(3436.0, atr=6.1, strength=TrendStrength.STRONG, swing_low=3419.8)
        stop = chandelier_stop(managed, snap, 2.5, management())
        self.assertAlmostEqual(stop, 3438.2 - 2.5 * 6.1, places=6)

    def test_structure_floor_tightens_a_loose_chandelier(self):
        managed = trade(tp1_done=True, breakeven_done=True, remaining_size=0.5,
                        stop_level=3400.0, best_price=3438.2)
        snap = snapshot(3436.0, atr=6.1, strength=TrendStrength.STRONG, swing_low=3430.0)
        stop = chandelier_stop(managed, snap, 2.5, management())
        # swing low 3430 minus a 0.25*ATR buffer beats the 3422.95 chandelier.
        self.assertAlmostEqual(stop, 3430.0 - 0.25 * 6.1, places=6)

    def test_the_stop_never_loosens(self):
        managed = trade(tp1_done=True, breakeven_done=True, remaining_size=0.5,
                        stop_level=3423.0, best_price=3441.0)
        # A cooling trend widens k, which would push the chandelier back down.
        result = evaluate(
            managed,
            snapshot(3437.0, atr=6.1, strength=TrendStrength.MODERATE, swing_low=3419.8),
            management(extend_tp3=False),
        )
        self.assertNotIn(DecisionKind.SET_STOP, kinds(result))

    def test_a_weak_trend_holds_the_stop_where_it_is(self):
        managed = trade(tp1_done=True, breakeven_done=True, remaining_size=0.5,
                        stop_level=3400.0, best_price=3430.0)
        result = evaluate(
            managed,
            snapshot(3428.0, atr=4.0, strength=TrendStrength.WEAK, swing_low=3420.0),
            management(trail_when_weak=False, extend_tp3=False),
        )
        self.assertNotIn(DecisionKind.SET_STOP, kinds(result))
        self.assertTrue(any("WEAK" in note.reason for note in result.blocked))

    def test_trailing_never_gives_back_break_even(self):
        managed = trade(tp1_done=True, breakeven_done=True, remaining_size=0.5,
                        stop_level=3400.0, best_price=3412.0)
        result = evaluate(
            managed,
            snapshot(3411.0, atr=6.0, strength=TrendStrength.STRONG, swing_low=3380.0),
            management(extend_tp3=False),
        )
        # The raw chandelier (3412 - 15) sits below entry; it must be clamped out.
        self.assertNotIn(DecisionKind.SET_STOP, kinds(result))

    def test_a_stop_inside_the_brokers_minimum_distance_is_blocked(self):
        managed = trade(tp1_done=True, breakeven_done=True, remaining_size=0.5,
                        stop_level=3400.0, best_price=3410.2)
        snap = snapshot(3410.2, atr=0.1, strength=TrendStrength.STRONG, swing_low=3410.1)
        result = evaluate(managed, snap, management(extend_tp3=False))
        self.assertNotIn(DecisionKind.SET_STOP, kinds(result))
        self.assertTrue(any("minimum" in note.reason for note in result.blocked))

    def test_break_even_and_trailing_collapse_into_one_modification(self):
        # Both would set a stop in the same cycle; only the better one is sent.
        managed = trade(best_price=3426.0)
        snap = snapshot(3424.0, atr=4.0, strength=TrendStrength.STRONG, swing_low=3415.0)
        result = evaluate(managed, snap, management(extend_tp3=False))
        stops = [d for d in result.decisions if d.kind is DecisionKind.SET_STOP]
        self.assertEqual(len(stops), 1)
        self.assertGreater(stops[0].stop_level, managed.entry_price)


class TargetExtensionTests(unittest.TestCase):
    def test_a_strong_trend_pushes_tp3_out_instead_of_exiting(self):
        managed = trade(tp1_done=True, tp2_done=True, breakeven_done=True,
                        remaining_size=0.25, stop_level=3410.0, best_price=3430.5)
        result = evaluate(
            managed,
            snapshot(3429.5, atr=4.0, strength=TrendStrength.STRONG, swing_low=3420.0),
            management(),
        )
        target = first(result, DecisionKind.SET_TARGET)
        self.assertEqual(target.profit_level, 3434.0)  # TP3 3430 + 1.0 ATR
        self.assertNotIn(DecisionKind.CLOSE_ALL, kinds(result))

    def test_extensions_are_capped(self):
        managed = trade(tp1_done=True, tp2_done=True, breakeven_done=True,
                        remaining_size=0.25, stop_level=3410.0, best_price=3431.0,
                        tp3_extensions=3)
        result = evaluate(
            managed,
            snapshot(3431.0, atr=4.0, strength=TrendStrength.STRONG, swing_low=3425.0),
            management(tp3_max_extensions=3),
        )
        self.assertNotIn(DecisionKind.SET_TARGET, kinds(result))
        self.assertIn(DecisionKind.CLOSE_ALL, kinds(result))

    def test_a_moderate_trend_takes_tp3_rather_than_extending(self):
        managed = trade(tp1_done=True, tp2_done=True, breakeven_done=True,
                        remaining_size=0.25, stop_level=3410.0, best_price=3431.0)
        result = evaluate(
            managed,
            snapshot(3431.0, atr=4.0, strength=TrendStrength.MODERATE, swing_low=3425.0),
            management(),
        )
        self.assertIn(DecisionKind.CLOSE_ALL, kinds(result))


if __name__ == "__main__":
    unittest.main()
