"""End-to-end lifecycle: detect -> confirm -> ladder -> break-even -> trail -> close."""

from __future__ import annotations

import unittest
from datetime import timedelta

from tmbot.broker.paper import PaperBroker
from tmbot.config import AnalysisConfig, Config, EpicConfig
from tmbot.manage.supervisor import PAUSED_KEY, Supervisor
from tmbot.models import Direction, TradeStatus, utcnow
from tmbot.notify.base import NullNotifier
from tmbot.store import Store
from tests.helpers import RULES, candles, management, position


def build_supervisor(**management_overrides):
    broker = PaperBroker()
    broker.set_rules(RULES)
    broker.set_quote("GOLD", 3400.0, 3400.2)
    # Enough history for the analytical read on every timeframe it asks for.
    for timeframe in ("H4", "H1", "M15"):
        broker.set_candles("GOLD", timeframe, candles(300, end=3400.0, drift=0.4, wave=5.0))

    config = Config()
    config.broker.environment = "demo"
    config.analysis = AnalysisConfig(watchlist=[EpicConfig(epic="GOLD", display="Gold")])
    config.management = management(**management_overrides)
    config.llm.enabled = False
    config.news.provider = "none"
    config.report.intraday_refresh_hours = 0
    config.report.charts = False   # drawn in test_chart.py, not on every tick  # keep the scheduler out of these tests

    store = Store(":memory:")
    notifier = NullNotifier()
    return Supervisor(broker, store, config, notifier), broker, store, notifier


def texts(notifier):
    return "\n".join(message for _, message in notifier.messages)


def set_levels(store, trade, *, tp1, tp2, tp3, sl):
    """Pin the adopted trade to known levels so price moves are deterministic."""
    trade.tp1, trade.tp2, trade.tp3, trade.sl = tp1, tp2, tp3, sl
    trade.stop_level = sl
    store.save_trade(trade)
    return trade


class AdoptionTests(unittest.TestCase):
    def test_a_new_position_waits_for_confirmation_before_anything_is_touched(self):
        supervisor, broker, store, notifier = build_supervisor()
        supervisor.start()
        broker.seed_position(position())

        supervisor.tick()

        pending = store.pending_trades()
        self.assertEqual(len(pending), 1)
        self.assertIs(pending[0].status, TradeStatus.PENDING_CONFIRMATION)
        self.assertIn("/confirm", texts(notifier))
        self.assertIsNone(broker.position("deal-000001").stop_level,
                          "nothing may be sent before the user confirms")

    def test_confirming_installs_the_plan_stop_and_final_target(self):
        supervisor, broker, store, notifier = build_supervisor()
        supervisor.start()
        broker.seed_position(position())
        supervisor.tick()
        trade = store.pending_trades()[0]

        reply = supervisor.confirm(trade.short_id)

        self.assertIn("Managing", reply)
        updated = store.trade(trade.deal_id)
        self.assertIs(updated.status, TradeStatus.MANAGING)
        self.assertEqual(broker.position("deal-000001").stop_level, updated.sl)
        self.assertEqual(broker.position("deal-000001").profit_level, updated.tp3)

    def test_declining_leaves_the_position_alone_for_good(self):
        supervisor, broker, store, _ = build_supervisor()
        supervisor.start()
        broker.seed_position(position())
        supervisor.tick()
        trade = store.pending_trades()[0]

        supervisor.decline(trade.short_id)
        supervisor.tick()  # a second cycle must not re-offer it

        self.assertIs(store.trade(trade.deal_id).status, TradeStatus.DECLINED)
        self.assertEqual(store.pending_trades(), [])
        self.assertIsNone(broker.position("deal-000001").stop_level)

    def test_an_unanswered_offer_expires_instead_of_lingering(self):
        supervisor, broker, store, notifier = build_supervisor(
            adoption_confirm_timeout_minutes=30
        )
        supervisor.start()
        broker.seed_position(position())
        supervisor.tick()

        trade = store.pending_trades()[0]
        trade.adopted_at = utcnow() - timedelta(minutes=31)
        store.save_trade(trade)
        supervisor.tick()

        self.assertIs(store.trade(trade.deal_id).status, TradeStatus.DECLINED)
        self.assertIn("not confirmed within", texts(notifier))

    def test_an_entry_against_the_bias_still_gets_levels_on_the_right_side(self):
        # The fixture's series reads bearish. Buying into it must not hand the
        # trade a stop above the entry and targets below it.
        supervisor, broker, store, _ = build_supervisor()
        supervisor.start()
        plan = supervisor._plan_for("GOLD")
        self.assertIs(plan.direction, Direction.SELL, "fixture precondition")

        broker.seed_position(position(direction=Direction.BUY, entry=3400.0))
        supervisor.tick()
        trade = store.pending_trades()[0]

        self.assertLess(trade.sl, trade.entry_price, "a long's stop belongs below the entry")
        self.assertGreater(trade.tp1, trade.entry_price)
        self.assertGreater(trade.tp2, trade.tp1)
        self.assertGreater(trade.tp3, trade.tp2)

    def test_the_plan_is_rebased_onto_the_actual_fill(self):
        supervisor, broker, store, _ = build_supervisor()
        supervisor.start()
        plan = supervisor._plan_for("GOLD")
        # Trade the side the plan argues for, so this isolates rebasing.
        broker.seed_position(
            position(direction=plan.direction, entry=plan.reference_price + 5.0)
        )

        supervisor.tick()
        trade = store.pending_trades()[0]

        # Targets are structural and stay put; risk distance is preserved.
        self.assertEqual(trade.tp1, plan.tp1)
        self.assertAlmostEqual(trade.initial_risk, abs(plan.reference_price - plan.sl), places=6)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.supervisor, self.broker, self.store, self.notifier = build_supervisor(
            extend_tp3=False
        )
        self.supervisor.start()
        self.broker.seed_position(position(size=2.0, entry=3400.0))
        self.supervisor.tick()
        trade = self.store.pending_trades()[0]
        self.supervisor.confirm(trade.short_id)
        self.trade = set_levels(
            self.store, self.store.trade(trade.deal_id),
            tp1=3410.0, tp2=3420.0, tp3=3430.0, sl=3390.0,
        )

    def move(self, price):
        self.broker.set_quote("GOLD", price, price + 0.2)
        self.supervisor.tick()
        return self.store.trade(self.trade.deal_id)

    def test_the_full_ladder_runs_in_order(self):
        # TP1: half off, stop to entry.
        trade = self.move(3410.5)
        self.assertTrue(trade.tp1_done)
        self.assertTrue(trade.breakeven_done)
        self.assertEqual(self.broker.position(trade.deal_id).size, 1.0)
        self.assertGreaterEqual(
            self.broker.position(trade.deal_id).stop_level, 3400.0,
            "the stop must be at the entry price or better once TP1 is hit",
        )

        # TP2: another quarter of the original off, 25% left running.
        trade = self.move(3420.5)
        self.assertTrue(trade.tp2_done)
        self.assertEqual(self.broker.position(trade.deal_id).size, 0.5)

        # TP3: the runner is closed out.
        trade = self.move(3430.5)
        self.assertIs(trade.status, TradeStatus.CLOSED)
        self.assertIsNone(self.broker.position(self.trade.deal_id))

    def test_the_stop_never_moves_back_down_as_price_retraces(self):
        self.move(3410.5)
        stop_after_tp1 = self.broker.position(self.trade.deal_id).stop_level
        self.move(3405.0)
        self.assertEqual(self.broker.position(self.trade.deal_id).stop_level, stop_after_tp1)

    def test_paused_management_evaluates_but_sends_nothing(self):
        self.store.set(PAUSED_KEY, "1")
        trade = self.move(3410.5)
        self.assertFalse(trade.tp1_done)
        self.assertEqual(self.broker.position(trade.deal_id).size, 2.0)

        self.store.set(PAUSED_KEY, "0")
        trade = self.move(3410.5)
        self.assertTrue(trade.tp1_done)

    def test_a_position_closed_at_the_broker_is_reconciled(self):
        self.broker._positions.clear()
        self.supervisor.tick()
        self.assertIs(self.store.trade(self.trade.deal_id).status, TradeStatus.CLOSED)
        self.assertIn("is closed at the broker", texts(self.notifier))

    def test_status_reports_the_ladder_state(self):
        self.move(3410.5)
        status = self.supervisor.status_text()
        self.assertIn("GOLD", status)
        self.assertIn(self.trade.short_id, status)
        self.assertIn("[1-B", status)   # TP1 done, break-even done

    def test_a_closed_market_is_left_alone(self):
        from tmbot.models import MarketRules
        self.broker.set_rules(MarketRules(
            epic="GOLD", min_deal_size=0.1, size_step=0.1,
            decimal_places=2, min_stop_distance=0.5, tradeable=False,
        ))

        trade = self.move(3410.5)   # well past TP1

        self.assertFalse(trade.tp1_done, "nothing should be sent while the market is shut")
        self.assertEqual(self.broker.position(trade.deal_id).size, 2.0)

    def test_a_manual_close_command_exits_the_position(self):
        self.supervisor.close_command(self.trade.short_id)
        self.assertIsNone(self.broker.position(self.trade.deal_id))


class TrailingLifecycleTests(unittest.TestCase):
    def test_a_strong_trend_trails_the_stop_up_behind_price(self):
        supervisor, broker, store, _ = build_supervisor(extend_tp3=False)
        # A clean, strong advance so ADX reads STRONG on the management timeframe.
        broker.set_candles("GOLD", "M15", candles(300, end=3410.0, drift=1.5, wave=0.5))
        supervisor.start()
        broker.seed_position(position(size=2.0, entry=3400.0))
        supervisor.tick()
        pending = store.pending_trades()[0]
        supervisor.confirm(pending.short_id)
        set_levels(store, store.trade(pending.deal_id),
                   tp1=3410.0, tp2=3480.0, tp3=3490.0, sl=3390.0)

        broker.set_quote("GOLD", 3412.0, 3412.2)
        supervisor.tick()
        after_tp1 = store.trade(pending.deal_id)
        self.assertTrue(after_tp1.breakeven_done)

        broker.set_quote("GOLD", 3440.0, 3440.2)
        supervisor.tick()
        trailed = store.trade(pending.deal_id)

        self.assertTrue(trailed.trailing_active)
        self.assertGreater(trailed.stop_level, after_tp1.stop_level)
        self.assertLess(trailed.stop_level, 3440.0)


if __name__ == "__main__":
    unittest.main()
