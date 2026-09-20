"""Three-deal exit model: one deal per target, closed whole."""

from __future__ import annotations

import unittest
from datetime import timedelta

from tmbot.broker.paper import PaperBroker
from tmbot.config import AnalysisConfig, Config, EpicConfig, ManagementConfig
from tmbot.manage.rules import DecisionKind, evaluate
from tmbot.manage.supervisor import Supervisor
from tmbot.models import Direction, Stage, TradeStatus
from tmbot.notify.base import NullNotifier
from tmbot.store import Store
from tests.helpers import BASE, RULES, candles, management, position, snapshot, trade


def kinds(evaluation):
    return [decision.kind for decision in evaluation.decisions]


def first(evaluation, kind):
    return next(d for d in evaluation.decisions if d.kind is kind)


def legs(**overrides):
    return management(exit_model="three_deals", **overrides)


class LegExitTests(unittest.TestCase):
    def test_the_first_leg_closes_entirely_at_tp1(self):
        leg = trade(leg_index=0, leg_target=Stage.TP1)
        result = evaluate(leg, snapshot(3410.0), legs())
        close = first(result, DecisionKind.CLOSE_ALL)
        self.assertEqual(close.size, 1.0, "the whole deal goes, not half of it")
        self.assertEqual(close.stage, Stage.TP1)
        self.assertNotIn(DecisionKind.PARTIAL_CLOSE, kinds(result))

    def test_the_second_leg_holds_through_tp1_and_closes_at_tp2(self):
        leg = trade(leg_index=1, leg_target=Stage.TP2)

        at_tp1 = evaluate(leg, snapshot(3410.0), legs())
        self.assertNotIn(DecisionKind.CLOSE_ALL, kinds(at_tp1))

        leg.breakeven_done = True
        leg.stop_level = 3400.0
        at_tp2 = evaluate(leg, snapshot(3420.0), legs())
        self.assertEqual(first(at_tp2, DecisionKind.CLOSE_ALL).size, 1.0)

    def test_the_third_leg_rides_past_both_and_closes_at_tp3(self):
        leg = trade(leg_index=2, leg_target=Stage.TP3, breakeven_done=True, stop_level=3400.0)
        for price in (3410.0, 3420.0):
            self.assertNotIn(
                DecisionKind.CLOSE_ALL, kinds(evaluate(leg, snapshot(price), legs()))
            )
        at_tp3 = evaluate(leg, snapshot(3430.5), legs(extend_tp3=False))
        self.assertEqual(first(at_tp3, DecisionKind.CLOSE_ALL).size, 1.0)

    def test_every_leg_goes_to_break_even_when_tp1_trades(self):
        for index, target in enumerate((Stage.TP1, Stage.TP2, Stage.TP3)):
            leg = trade(leg_index=index, leg_target=target)
            result = evaluate(leg, snapshot(3410.0), legs())
            stop = first(result, DecisionKind.SET_STOP)
            self.assertEqual(stop.stop_level, 3400.0, f"leg {index + 1} must be risk-free")

    def test_only_the_runner_is_trailed(self):
        strong = snapshot(3424.0, atr=4.0, strength=__import__(
            "tmbot.models", fromlist=["TrendStrength"]
        ).TrendStrength.STRONG, swing_low=3415.0)

        middle = trade(leg_index=1, leg_target=Stage.TP2, breakeven_done=True,
                       stop_level=3400.0, best_price=3426.0)
        self.assertNotIn(DecisionKind.SET_STOP, kinds(evaluate(middle, strong, legs())))

        runner = trade(leg_index=2, leg_target=Stage.TP3, breakeven_done=True,
                       stop_level=3400.0, best_price=3426.0)
        self.assertIn(DecisionKind.SET_STOP, kinds(evaluate(runner, strong, legs())))

    def test_only_the_runner_has_its_target_extended(self):
        from tmbot.models import TrendStrength
        strong = snapshot(3429.5, atr=4.0, strength=TrendStrength.STRONG, swing_low=3420.0)

        middle = trade(leg_index=1, leg_target=Stage.TP2, breakeven_done=True,
                       stop_level=3410.0, best_price=3430.5)
        self.assertNotIn(DecisionKind.SET_TARGET, kinds(evaluate(middle, strong, legs())))

        runner = trade(leg_index=2, leg_target=Stage.TP3, breakeven_done=True,
                       stop_level=3410.0, best_price=3430.5)
        self.assertEqual(
            first(evaluate(runner, strong, legs()), DecisionKind.SET_TARGET).profit_level,
            3434.0,
        )


def build_supervisor(**management_overrides):
    broker = PaperBroker()
    broker.set_rules(RULES)
    broker.hedging = True          # three deals only stay separate when hedging is on
    broker.set_quote("GOLD", 3400.0, 3400.2)
    for timeframe in ("H4", "H1", "M15"):
        broker.set_candles("GOLD", timeframe, candles(300, end=3400.0, drift=0.4, wave=5.0))

    config = Config()
    config.broker.environment = "demo"
    config.analysis = AnalysisConfig(watchlist=[EpicConfig(epic="GOLD")])
    config.management = legs(**management_overrides)
    config.llm.enabled = False
    config.report.intraday_refresh_hours = 0

    store = Store(":memory:")
    notifier = NullNotifier()
    return Supervisor(broker, store, config, notifier), broker, store, notifier


class BasketLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.supervisor, self.broker, self.store, self.notifier = build_supervisor(
            extend_tp3=False
        )
        self.supervisor.start()
        for number in (1, 2, 3):
            self.broker.seed_position(position(deal_id=f"deal-{number}", size=1.0, entry=3400.0))
        self.supervisor.tick()
        self.pin_levels()

    def pin_levels(self):
        for leg in self.store.trades_with_status(
            TradeStatus.MANAGING, TradeStatus.PENDING_CONFIRMATION
        ):
            leg.tp1, leg.tp2, leg.tp3, leg.sl = 3410.0, 3420.0, 3430.0, 3390.0
            leg.stop_level = 3390.0
            self.store.save_trade(leg)

    def move(self, price):
        self.broker.set_quote("GOLD", price, price + 0.2)
        self.supervisor.tick()

    def test_three_deals_become_one_basket_with_one_target_each(self):
        pending = self.store.pending_trades()
        self.assertEqual(len(pending), 3)
        self.assertEqual(len({leg.group_id for leg in pending}), 1, "one basket, not three")
        self.assertEqual(
            sorted(leg.leg_target.value for leg in pending), ["TP1", "TP2", "TP3"]
        )

    def test_one_confirmation_adopts_all_three(self):
        reply = self.supervisor.confirm(self.store.pending_trades()[0].short_id)
        self.assertEqual(reply.count("Managing"), 3)
        self.assertEqual(len(self.store.pending_trades()), 0)
        self.assertEqual(len(self.store.active_trades()), 3)

    def test_each_target_closes_exactly_one_deal(self):
        self.supervisor.confirm(self.store.pending_trades()[0].short_id)
        by_target = {
            leg.leg_target.value: leg.deal_id for leg in self.store.active_trades()
        }

        self.move(3410.5)
        self.assertIsNone(self.broker.position(by_target["TP1"]), "TP1 leg is gone")
        self.assertEqual(self.broker.position(by_target["TP2"]).size, 1.0, "TP2 leg untouched")
        self.assertEqual(self.broker.position(by_target["TP3"]).size, 1.0, "TP3 leg untouched")
        for target in ("TP2", "TP3"):
            self.assertEqual(
                self.broker.position(by_target[target]).stop_level, 3400.0,
                f"{target} leg should be risk-free once TP1 trades",
            )

        self.move(3420.5)
        self.assertIsNone(self.broker.position(by_target["TP2"]))
        self.assertEqual(self.broker.position(by_target["TP3"]).size, 1.0)

        self.move(3430.5)
        self.assertIsNone(self.broker.position(by_target["TP3"]))
        self.assertTrue(all(
            leg.status is TradeStatus.CLOSED
            for leg in self.store.trades_with_status(TradeStatus.CLOSED)
        ))

    def test_a_fourth_deal_starts_a_new_basket(self):
        self.supervisor.confirm(self.store.pending_trades()[0].short_id)
        self.broker.seed_position(position(deal_id="deal-4", size=1.0, entry=3402.0))
        self.supervisor.tick()
        extra = self.store.trade("deal-4")
        first_group = self.store.active_trades()[0].group_id
        self.assertNotEqual(extra.group_id, first_group)
        self.assertEqual(extra.leg_target, Stage.TP1)

    def test_a_late_leg_joins_an_already_confirmed_basket(self):
        # Only two deals filled at first; confirm, then the third arrives.
        supervisor, broker, store, _ = build_supervisor()
        supervisor.start()
        for number in (1, 2):
            broker.seed_position(position(deal_id=f"deal-{number}", size=1.0, entry=3400.0))
        supervisor.tick()
        supervisor.confirm(store.pending_trades()[0].short_id)

        broker.seed_position(position(deal_id="deal-3", size=1.0, entry=3400.0))
        supervisor.tick()

        late = store.trade("deal-3")
        self.assertIs(late.status, TradeStatus.MANAGING, "adopted without asking again")
        self.assertEqual(late.leg_target, Stage.TP3)
        self.assertEqual(late.group_id, store.trade("deal-1").group_id)


class DefaultsTests(unittest.TestCase):
    def test_the_project_ships_in_three_deal_mode(self):
        self.assertEqual(ManagementConfig().exit_model, "three_deals")
        self.assertEqual(ManagementConfig().leg_targets, ["TP1", "TP2", "TP3"])

    def test_an_incomplete_basket_says_what_is_still_missing(self):
        supervisor, broker, store, notifier = build_supervisor()
        supervisor.start()
        broker.seed_position(position(deal_id="deal-1", size=1.0, entry=3400.0))
        supervisor.tick()
        offer = "\n".join(text for _, text in notifier.messages)
        self.assertIn("WAITING on 2 more deal(s)", offer)
        self.assertIn("closes at TP1 on its own", offer)


class GroupingTests(unittest.TestCase):
    def test_deals_are_grouped_by_closeness_to_each_other_not_by_age(self):
        # The bot starts long after the deals were placed; they must still
        # group, because they were opened seconds apart.
        supervisor, broker, store, _ = build_supervisor()
        supervisor.start()
        for number in (1, 2, 3):
            broker.seed_position(position(deal_id=f"deal-{number}", size=1.0, entry=3400.0))
        supervisor.tick()
        self.assertEqual(len({leg.group_id for leg in store.pending_trades()}), 1)

    def test_deals_placed_far_apart_are_separate_trades(self):
        supervisor, broker, store, _ = build_supervisor(group_window_minutes=15)
        supervisor.start()
        broker.seed_position(position(deal_id="deal-1", size=1.0, entry=3400.0))
        supervisor.tick()

        later = position(deal_id="deal-2", size=1.0, entry=3400.0)
        object.__setattr__(later, "created_at", BASE + timedelta(hours=3))
        broker.seed_position(later)
        supervisor.tick()

        self.assertEqual(len({leg.group_id for leg in store.pending_trades()}), 2)

    def test_a_netting_account_is_called_out_at_startup(self):
        supervisor, broker, store, notifier = build_supervisor()
        broker.hedging = False
        supervisor.start()
        self.assertTrue(any(
            "hedging OFF" in text for level, text in notifier.messages if level == "error"
        ))


if __name__ == "__main__":
    unittest.main()
