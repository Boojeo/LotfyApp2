"""Hard risk limit: the whole trade loses at most N% of equity at its stop."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tmbot.analysis import risk
from tmbot.models import MarketRules, Stage, TradeStatus
from tests.helpers import RULES, position
from tests.test_audit_fixes import leg
from tests.test_supervisor import build_supervisor

# Contract specs as an Exness account reports them (value of a 1.0 move on 1 lot).
GOLD = MarketRules(epic="XAUUSDm", min_deal_size=0.01, size_step=0.01,
                   decimal_places=3, value_per_point=100.0, max_deal_size=200.0)
EURUSD = MarketRules(epic="EURUSDm", min_deal_size=0.01, size_step=0.01,
                     decimal_places=5, value_per_point=100_000.0, max_deal_size=200.0)
OIL = MarketRules(epic="USOILm", min_deal_size=0.01, size_step=0.01,
                  decimal_places=3, value_per_point=1_000.0, max_deal_size=100.0)


def assess(**overrides):
    args = dict(equity=10_000.0, currency="USD", risk_percent=1.0,
                stop_distance=5.0, rules=GOLD, legs_planned=3)
    args.update(overrides)
    return risk.assess(**args)


class LimitTests(unittest.TestCase):
    def test_one_percent_of_equity_is_the_ceiling_for_the_whole_trade(self):
        result = assess()
        self.assertEqual(result.max_money, 100.0)
        self.assertLessEqual(result.total_money, result.max_money)
        self.assertTrue(result.within_limit)

    def test_the_limit_is_for_all_legs_together_not_each(self):
        three, one = assess(legs_planned=3), assess(legs_planned=1)
        self.assertAlmostEqual(three.selected_volume, 0.18)
        self.assertAlmostEqual(one.selected_volume, 0.2)
        self.assertLess(three.total_money, 100.0 + 1e-9, "never 3 x 1%")

    def test_equity_not_a_fixed_number_sets_the_size(self):
        self.assertEqual(assess(equity=986.36).max_money, 9.86)
        self.assertLess(assess(equity=5_000).selected_volume, assess().selected_volume)

    def test_same_lots_risk_different_money_per_instrument(self):
        for rules, distance, expected_per_lot in (
            (GOLD, 5.0, 500.0),        # $5 on 100 oz
            (EURUSD, 0.0020, 200.0),   # 20 pips on 100,000
            (OIL, 0.50, 500.0),        # 50 cents on 1,000 barrels
        ):
            result = assess(rules=rules, stop_distance=distance)
            self.assertAlmostEqual(result.loss_per_lot, expected_per_lot, places=4)
            self.assertLessEqual(result.total_money, 100.0)

    def test_volume_is_rounded_down_to_the_step_never_up(self):
        result = assess(stop_distance=3.0)   # ideal 0.3333 total
        self.assertEqual(result.per_leg, 0.11)
        self.assertLessEqual(result.total_money, 100.0)

    def test_the_brokers_maximum_lot_caps_each_leg(self):
        result = assess(equity=10_000_000.0, rules=replace(GOLD, max_deal_size=5.0))
        self.assertEqual(result.per_leg, 5.0)

    def test_the_spread_is_part_of_the_loss(self):
        self.assertGreater(assess(spread=1.0).loss_per_lot, assess().loss_per_lot)


class MinimumLotTests(unittest.TestCase):
    def test_too_small_for_three_legs_advises_two(self):
        result = assess(equity=1_000.0, stop_distance=4.0)   # $10 allowed, $4 per 0.01
        self.assertEqual(result.verdict, risk.REDUCED_LEGS)
        self.assertEqual((result.legs, result.per_leg), (2, 0.01))
        self.assertEqual(result.total_money, 8.0)

    def test_three_minimum_lots_that_fit_stay_three(self):
        result = assess(equity=1_000.0, stop_distance=3.0)   # 3 x $3 = $9 <= $10
        self.assertEqual((result.verdict, result.legs), (risk.OK, 3))

    def test_too_small_for_two_advises_one(self):
        result = assess(equity=1_000.0, stop_distance=8.0)   # $10 allowed, $8 per 0.01
        self.assertEqual((result.verdict, result.legs), (risk.REDUCED_LEGS, 1))
        self.assertEqual(result.total_money, 8.0)

    def test_even_one_minimum_lot_over_the_limit_is_rejected_not_rounded_up(self):
        # The brief's example: $9.86 allowed, the minimum lot would lose ~$30.
        result = assess(equity=986.36, stop_distance=30.0)
        self.assertEqual(result.verdict, risk.REJECTED)
        self.assertEqual((result.legs, result.selected_volume), (0, 0.0))
        self.assertFalse(result.within_limit)
        self.assertEqual(result.min_lot_money, 30.0)
        self.assertGreater(result.min_lot_percent, 3.0)

    def test_unknown_inputs_are_not_treated_as_safe(self):
        self.assertIsNone(assess(rules=RULES))          # no contract value
        self.assertIsNone(assess(equity=0.0))           # no equity


class ReportVerdictTests(unittest.TestCase):
    def test_a_strong_signal_cannot_override_the_risk_limit(self):
        from tmbot.analysis.report import render_text
        from tests.test_style import broker_with, auto_config, UP
        from tmbot.analysis.report import ReportBuilder
        broker = broker_with(M15=UP, H1=UP)
        broker.set_rules(replace(GOLD, epic="GOLD"))
        broker.account_summary = lambda: {"currency": "USD", "equity": 50.0}
        plan = ReportBuilder(broker, auto_config()).build("GOLD")
        self.assertEqual(plan.sizing["verdict"], "REJECTED")
        self.assertIn("REJECTED -- RISK LIMIT", render_text(plan))


class BasketTests(unittest.TestCase):
    """Fewer deals advised -> adopted with the targets you chose for that size."""

    def setUp(self):
        self.supervisor, self.broker, self.store, self.notifier = build_supervisor(
            exit_model="three_deals"
        )
        self.broker.set_rules(replace(GOLD, epic="GOLD", decimal_places=2))
        self.supervisor.start()

    def adopt_with_advice(self, legs, *deals):
        from tmbot.models import Direction
        plan = self.supervisor._plan_for("GOLD", force=True, direction=Direction.BUY)
        plan.sizing = {"verdict": "REDUCED_LEGS" if legs < 3 else "OK", "legs": legs}
        self.store.save_plan(plan)
        for deal in deals:
            self.broker.seed_position(replace(deal, size=0.01))
        self.supervisor.tick()
        return {t.deal_id: t for t in self.store.pending_trades()}

    def test_two_deals_close_at_tp1_and_tp3(self):
        trades = self.adopt_with_advice(2, leg("101", 0), leg("102", 1))
        self.assertIs(trades["101"].leg_target, Stage.TP1)
        self.assertIs(trades["102"].leg_target, Stage.TP3)
        self.assertTrue(trades["102"].is_runner)

    def test_one_deal_closes_at_tp1(self):
        trades = self.adopt_with_advice(1, leg("101", 0))
        self.assertIs(trades["101"].leg_target, Stage.TP1)
        self.assertEqual(trades["101"].basket_size, 1)

    def test_a_basket_keeps_its_size_when_the_advice_changes(self):
        self.adopt_with_advice(2, leg("101", 0))
        trades = self.adopt_with_advice(3, leg("102", 1))
        self.assertIs(trades["102"].leg_target, Stage.TP3, "still the 2-deal basket")

    def test_your_actual_risk_is_shown_before_you_confirm(self):
        self.adopt_with_advice(3, leg("101", 0))
        messages = "\n".join(text for _, text in self.notifier.messages)
        self.assertIn("at the stop", messages)
        self.assertIn("of equity", messages)

    def test_over_the_limit_is_a_loud_warning_not_silence(self):
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 100.0}
        self.adopt_with_advice(3, leg("101", 0))
        messages = "\n".join(text for _, text in self.notifier.messages)
        self.assertIn("OVER your 1% limit", messages)
        trade = self.store.pending_trades()[0]
        self.assertIs(trade.status, TradeStatus.PENDING_CONFIRMATION,
                      "the bot never resizes or refuses your deal on its own")


if __name__ == "__main__":
    unittest.main()
