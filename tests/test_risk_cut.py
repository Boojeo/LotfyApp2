"""Halfway stop: part of the way to TP1, keep only part of the risk."""

from __future__ import annotations

import unittest

from tmbot.config import Config, ConfigError
from tmbot.manage.rules import DecisionKind, evaluate
from tmbot.models import Direction
from tests.helpers import management, snapshot, trade


def stops(evaluation):
    return [d for d in evaluation.decisions if d.kind is DecisionKind.SET_STOP]


class HalfwayStopTests(unittest.TestCase):
    # Long from 3400, stop 3390 (risk 10), TP1 3410: halfway is 3405.

    def test_before_halfway_the_stop_stays_put(self):
        self.assertEqual(stops(evaluate(trade(), snapshot(3404.0), management())), [])

    def test_halfway_to_tp1_halves_the_risk(self):
        [stop] = stops(evaluate(trade(), snapshot(3405.5), management()))
        self.assertEqual(stop.stop_level, 3395.0, "10 of risk becomes 5")
        self.assertEqual(stop.reason_key, "reason.risk_cut")

    def test_it_works_for_a_short(self):
        short = trade(Direction.SELL)   # entry 3400, stop 3410, TP1 3390
        [stop] = stops(evaluate(short, snapshot(3394.5), management()))
        self.assertEqual(stop.stop_level, 3405.0)

    def test_it_happens_once_and_never_loosens(self):
        moved = trade(stop_level=3395.0)
        self.assertEqual(stops(evaluate(moved, snapshot(3407.0), management())), [])
        tighter = trade(stop_level=3398.0)   # you already tightened it yourself
        self.assertEqual(stops(evaluate(tighter, snapshot(3407.0), management())), [])

    def test_break_even_at_tp1_still_takes_over(self):
        [stop] = stops(evaluate(trade(), snapshot(3410.5), management()))
        self.assertEqual(stop.stop_level, 3400.0)
        self.assertEqual(stop.reason_key, "reason.breakeven")

    def test_every_leg_of_a_basket_gets_it(self):
        from tmbot.models import Stage
        for index, target in enumerate((Stage.TP1, Stage.TP2, Stage.TP3)):
            leg = trade(leg_index=index, leg_target=target)
            [stop] = stops(evaluate(leg, snapshot(3405.5),
                                    management(exit_model="three_deals")))
            self.assertEqual(stop.stop_level, 3395.0, f"leg {index + 1}")

    def test_the_amounts_are_settings(self):
        config = management(risk_cut_at=0.75, risk_cut_to=0.25)
        self.assertEqual(stops(evaluate(trade(), snapshot(3407.0), config)), [])
        [stop] = stops(evaluate(trade(), snapshot(3407.6), config))
        self.assertEqual(stop.stop_level, 3397.5)

    def test_it_can_be_switched_off(self):
        self.assertEqual(
            stops(evaluate(trade(), snapshot(3407.0), management(risk_cut_at=0))), []
        )

    def test_nonsense_settings_are_refused(self):
        config = Config()
        config.broker.environment = "demo"
        config.management.risk_cut_at = 1.5
        with self.assertRaises(ConfigError):
            config.validate(connecting=False)


if __name__ == "__main__":
    unittest.main()
