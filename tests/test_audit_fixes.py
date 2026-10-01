"""Fixes from the audit: per-leg broker targets, leg order, direction."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from tmbot.models import Direction, Stage, TradeStatus
from tests.helpers import BASE, position
from tests.test_supervisor import build_supervisor


def leg(deal_id: str, seconds: int, direction: Direction = Direction.BUY):
    return replace(position(deal_id=deal_id, direction=direction),
                   created_at=BASE + timedelta(seconds=seconds))


class ThreeDealTargetTests(unittest.TestCase):
    def setUp(self):
        self.supervisor, self.broker, self.store, _ = build_supervisor(
            exit_model="three_deals"
        )
        self.supervisor.start()

    def adopt(self, *positions):
        for item in positions:
            self.broker.seed_position(item)
        self.supervisor.tick()
        self.supervisor.confirm(self.store.pending_trades()[0].short_id)
        return {trade.deal_id: trade for trade in self.store.active_trades()}

    def test_each_leg_carries_its_own_target_at_the_broker(self):
        trades = self.adopt(leg("101", 0), leg("102", 1), leg("103", 2))
        for deal_id, stage in (("101", Stage.TP1), ("102", Stage.TP2), ("103", Stage.TP3)):
            trade = trades[deal_id]
            self.assertIs(trade.leg_target, stage)
            expected = {Stage.TP1: trade.tp1, Stage.TP2: trade.tp2, Stage.TP3: trade.tp3}[stage]
            self.assertEqual(self.broker.position(deal_id).profit_level, expected,
                             f"leg {deal_id} must rest at {stage.value} at the broker")

    def test_placing_a_leg_target_does_not_rewrite_tp3(self):
        trades = self.adopt(leg("101", 0), leg("102", 1), leg("103", 2))
        first = trades["101"]
        self.assertNotEqual(first.tp3, first.tp1, "TP3 must not become TP1")
        for trade in trades.values():
            self.assertEqual(trade.tp3_extensions, 0, "no extension used at adoption")

    def test_shuffled_broker_order_still_numbers_legs_by_opening_time(self):
        trades = self.adopt(leg("103", 2), leg("101", 0), leg("102", 1))
        self.assertIs(trades["101"].leg_target, Stage.TP1)
        self.assertIs(trades["102"].leg_target, Stage.TP2)
        self.assertIs(trades["103"].leg_target, Stage.TP3)

    def test_same_second_deals_are_ordered_by_ticket(self):
        trades = self.adopt(leg("1003", 0), leg("1001", 0), leg("1002", 0))
        self.assertIs(trades["1001"].leg_target, Stage.TP1)
        self.assertIs(trades["1003"].leg_target, Stage.TP3)

    def test_a_short_basket_gets_short_levels(self):
        trades = self.adopt(leg("201", 0, Direction.SELL), leg("202", 1, Direction.SELL),
                            leg("203", 2, Direction.SELL))
        for trade in trades.values():
            self.assertIs(trade.direction, Direction.SELL)
            self.assertGreater(trade.sl, trade.entry_price, "a short's stop is above")
            self.assertLess(trade.tp1, trade.entry_price)


class ManageDirectionTests(unittest.TestCase):
    def test_manage_builds_levels_for_the_side_actually_traded(self):
        supervisor, broker, store, _ = build_supervisor()
        supervisor.start()
        # Whatever the day's bias, the levels must follow the position's side;
        # this case and the long one below cover both directions.
        broker.seed_position(position(deal_id="deal-777777", direction=Direction.SELL))

        supervisor.manage_existing("777777")

        trade = store.trade_by_short_id("777777")
        self.assertIs(trade.status, TradeStatus.MANAGING)
        self.assertGreater(trade.sl, trade.entry_price, "SHORT -> stop above entry")
        self.assertLess(trade.tp1, trade.entry_price)

    def test_a_long_is_still_managed_long(self):
        supervisor, broker, store, _ = build_supervisor()
        supervisor.start()
        broker.seed_position(position(deal_id="deal-888888", direction=Direction.BUY))
        supervisor.manage_existing("888888")
        trade = store.trade_by_short_id("888888")
        self.assertLess(trade.sl, trade.entry_price)
        self.assertGreater(trade.tp1, trade.entry_price)


if __name__ == "__main__":
    unittest.main()
