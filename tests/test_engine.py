"""Execution: idempotency, partial-close strategies and failure handling."""

from __future__ import annotations

import unittest

from tmbot.broker.base import PartialCloseStrategy
from tmbot.broker.paper import PaperBroker
from tmbot.config import Config
from tmbot.errors import PermanentError, RetryableError
from tmbot.manage.engine import TradeEngine
from tmbot.manage.rules import Decision, DecisionKind, Evaluation, evaluate
from tmbot.models import Direction, Stage, TradeStatus
from tmbot.notify.base import NullNotifier
from tmbot.store import Store
from tests.helpers import RULES, management, position, snapshot, trade


def build(**config_overrides):
    broker = PaperBroker()
    broker.set_rules(RULES)
    broker.set_quote("GOLD", 3400.0, 3400.2)
    broker.seed_position(position())
    broker.probe_partial_close()
    store = Store(":memory:")
    config = Config()
    for key, value in config_overrides.items():
        setattr(config, key, value)
    config.management = management()
    notifier = NullNotifier()
    return broker, store, TradeEngine(broker, store, config, notifier), notifier


class ExecutionTests(unittest.TestCase):
    def test_tp1_partial_and_break_even_reach_the_broker(self):
        broker, store, engine, _ = build()
        managed = trade()
        broker.set_quote("GOLD", 3410.0, 3410.2)

        applied = engine.apply(managed, evaluate(managed, snapshot(3410.0), management()))

        self.assertEqual(len(applied), 2)
        self.assertEqual(broker.position("deal-000001").size, 0.5)
        self.assertEqual(broker.position("deal-000001").stop_level, 3400.0)
        self.assertTrue(managed.tp1_done)
        self.assertTrue(managed.breakeven_done)

    def test_a_replayed_decision_is_not_sent_twice(self):
        broker, store, engine, _ = build()
        managed = trade()
        evaluation = evaluate(managed, snapshot(3410.0), management())

        engine.apply(managed, evaluation)
        closes_before = sum(1 for name, *_ in broker.calls if name in ("close_position",
                                                                      "open_position"))
        # Same evaluation replayed (a crash-and-restart, or a duplicated tick).
        engine.apply(managed, evaluation)
        closes_after = sum(1 for name, *_ in broker.calls if name in ("close_position",
                                                                     "open_position"))
        self.assertEqual(closes_before, closes_after)

    def test_netting_offset_is_used_when_the_account_nets(self):
        broker, store, engine, _ = build()
        self.assertIs(broker.partial_close_strategy, PartialCloseStrategy.NETTING_OFFSET)
        managed = trade()

        engine.apply(managed, evaluate(managed, snapshot(3410.0), management()))

        self.assertTrue(any(name == "open_position" for name, *_ in broker.calls))
        self.assertEqual(broker.position("deal-000001").size, 0.5)

    def test_delete_with_size_is_used_on_a_hedging_account(self):
        broker, store, engine, _ = build()
        broker.hedging = True
        broker.probe_partial_close()
        managed = trade()

        engine.apply(managed, evaluate(managed, snapshot(3410.0), management()))

        self.assertTrue(any(name == "close_position" for name, *_ in broker.calls))
        self.assertEqual(broker.position("deal-000001").size, 0.5)

    def test_a_retryable_failure_releases_the_claim_for_the_next_cycle(self):
        broker, store, engine, _ = build()
        managed = trade()
        evaluation = evaluate(managed, snapshot(3410.0), management())
        broker.fail_next = RetryableError("connection reset")

        engine.apply(managed, evaluation)
        self.assertEqual(broker.position("deal-000001").size, 1.0)
        self.assertEqual(store.pending_actions(), [])

        # Next cycle: the same decision runs cleanly.
        engine.apply(managed, evaluation)
        self.assertEqual(broker.position("deal-000001").size, 0.5)

    def test_a_permanent_rejection_is_flagged_loudly_and_stops_the_cycle(self):
        broker, store, engine, notifier = build()
        managed = trade()
        broker.fail_next = PermanentError("error.invalid.stoploss", code="error.invalid.stoploss")

        engine.apply(managed, evaluate(managed, snapshot(3410.0), management()))

        self.assertIs(managed.status, TradeStatus.ERROR)
        self.assertTrue(any(level == "error" for level, _ in notifier.messages))
        # The failing decision halts the rest of the cycle rather than charging on.
        self.assertEqual(broker.position("deal-000001").stop_level, None)

    def test_an_over_close_disables_partials_and_warns(self):
        broker, store, engine, notifier = build()
        managed = trade(size=2.0, remaining_size=2.0)
        broker.seed_position(position(size=2.0))

        # Simulate a broker that ignores `size` and closes everything.
        original = broker.close_position
        broker.close_position = lambda deal_id, size=None: original(deal_id)
        broker.hedging = True
        broker.partial_close_strategy = PartialCloseStrategy.DELETE_WITH_SIZE

        engine.apply(managed, evaluate(managed, snapshot(3410.0), management()))

        self.assertIs(broker.partial_close_strategy, PartialCloseStrategy.UNSUPPORTED)
        self.assertTrue(any("whole position is gone" in text for _, text in notifier.messages))

    def test_dry_run_sends_nothing_but_advances_state(self):
        broker, store, engine, _ = build(dry_run=True)
        managed = trade()

        applied = engine.apply(managed, evaluate(managed, snapshot(3410.0), management()))

        self.assertEqual(len(applied), 2)
        self.assertEqual(broker.position("deal-000001").size, 1.0, "nothing should be sent")
        self.assertTrue(managed.tp1_done)
        self.assertTrue(managed.breakeven_done)

    def test_resync_remaps_a_position_that_changed_deal_id(self):
        broker, store, engine, _ = build()
        managed = trade()
        # The original deal is replaced by a smaller one, as a netting broker may do.
        broker._positions.clear()
        broker.seed_position(position(deal_id="deal-000002", size=0.5))

        found = engine.resync(managed)

        self.assertIsNotNone(found)
        self.assertEqual(managed.deal_id, "deal-000002")
        self.assertEqual(managed.remaining_size, 0.5)

    def test_close_all_marks_the_trade_closed(self):
        broker, store, engine, _ = build()
        managed = trade(tp1_done=True, tp2_done=True, remaining_size=0.3,
                        breakeven_done=True, stop_level=3400.0)
        broker.seed_position(position(size=0.3, stop=3400.0))

        engine.apply(managed, evaluate(managed, snapshot(3431.0),
                                       management(extend_tp3=False)))

        self.assertIs(managed.status, TradeStatus.CLOSED)
        self.assertIsNone(broker.position("deal-000001"))


class StoreTests(unittest.TestCase):
    def test_an_action_key_can_only_be_claimed_once(self):
        store = Store(":memory:")
        self.assertTrue(store.begin_action("k", "d", "PARTIAL_CLOSE"))
        self.assertFalse(store.begin_action("k", "d", "PARTIAL_CLOSE"))
        store.complete_action("k", "ref-1")
        self.assertFalse(store.begin_action("k", "d", "PARTIAL_CLOSE"))

    def test_a_released_claim_can_be_retried(self):
        store = Store(":memory:")
        store.begin_action("k", "d", "SET_STOP")
        store.release_action("k")
        self.assertTrue(store.begin_action("k", "d", "SET_STOP"))

    def test_trades_round_trip_through_sqlite(self):
        store = Store(":memory:")
        managed = trade(tp1_done=True, best_price=3421.5, tp3_extensions=2)
        store.save_trade(managed)
        loaded = store.trade(managed.deal_id)
        self.assertEqual(loaded.to_dict(), managed.to_dict())


if __name__ == "__main__":
    unittest.main()
