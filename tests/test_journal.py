"""Performance journal: fills are captured as trades close, and R is honest."""

from __future__ import annotations

import unittest
from datetime import timedelta

from tmbot.analysis import journal as journal_module
from tmbot.broker.paper import PaperBroker
from tmbot.config import Config, ReversalConfig
from tmbot.i18n import Translator
from tmbot.manage.engine import TradeEngine
from tmbot.manage.rules import evaluate
from tmbot.models import Direction, Fill, utcnow
from tmbot.notify.base import NullNotifier
from tmbot.store import Store
from tests.helpers import RULES, management, position, snapshot, trade


def fill(deal_id="d1", epic="GOLD", stage="TP1", price=3410.0, fraction=0.5,
         direction=Direction.BUY, entry=3400.0, risk=10.0, inferred=False, ago_days=0):
    return Fill(
        deal_id=deal_id, epic=epic, ts=utcnow() - timedelta(days=ago_days),
        stage=stage, size=1.0, price=price, entry_price=entry,
        direction=direction, initial_risk=risk, fraction=fraction, inferred=inferred,
    )


class RMultipleTests(unittest.TestCase):
    def test_r_is_weighted_by_how_much_of_the_trade_closed(self):
        # +1R move, but only half the position -- worth half an R.
        self.assertAlmostEqual(fill(price=3410.0, fraction=0.5).r_multiple, 0.5)
        self.assertAlmostEqual(fill(price=3410.0, fraction=1.0).r_multiple, 1.0)

    def test_a_loss_is_negative(self):
        self.assertAlmostEqual(fill(price=3390.0, fraction=1.0).r_multiple, -1.0)

    def test_a_short_earns_when_price_falls(self):
        self.assertAlmostEqual(
            fill(price=3390.0, fraction=1.0, direction=Direction.SELL).r_multiple, 1.0
        )

    def test_a_zero_risk_trade_scores_nothing_rather_than_dividing_by_zero(self):
        self.assertEqual(fill(risk=0.0).r_multiple, 0.0)

    def test_r_is_scale_free(self):
        # The same idea at 0.1 lots and 10 lots must score identically.
        small = Fill(deal_id="a", epic="GOLD", ts=utcnow(), stage="TP1", size=0.1,
                     price=3410.0, entry_price=3400.0, direction=Direction.BUY,
                     initial_risk=10.0, fraction=1.0)
        large = Fill(deal_id="b", epic="GOLD", ts=utcnow(), stage="TP1", size=10.0,
                     price=3410.0, entry_price=3400.0, direction=Direction.BUY,
                     initial_risk=10.0, fraction=1.0)
        self.assertEqual(small.r_multiple, large.r_multiple)


class CaptureTests(unittest.TestCase):
    def engine(self):
        broker = PaperBroker()
        broker.set_rules(RULES)
        broker.set_quote("GOLD", 3410.0, 3410.2)
        broker.seed_position(position(size=2.0))
        broker.probe_partial_close()
        store = Store(":memory:")
        config = Config()
        config.management = management()
        return TradeEngine(broker, store, config, NullNotifier()), store

    def test_a_partial_close_is_recorded_as_it_happens(self):
        engine, store = self.engine()
        managed = trade(size=2.0)
        engine.apply(managed, evaluate(managed, snapshot(3410.0), management()))

        fills = store.fills_for(managed.deal_id)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].stage, "TP1")
        self.assertAlmostEqual(fills[0].r_multiple, 0.5, places=6)
        self.assertAlmostEqual(managed.realised, 0.5, places=6)

    def test_the_same_exit_is_never_counted_twice(self):
        engine, store = self.engine()
        managed = trade(size=2.0)
        evaluation = evaluate(managed, snapshot(3410.0), management())
        engine.apply(managed, evaluation)
        engine.apply(managed, evaluation)   # a replay after a restart
        self.assertEqual(len(store.fills_for(managed.deal_id)), 1)

    def test_a_reversal_close_is_labelled_as_one(self):
        engine, store = self.engine()
        managed = trade(size=2.0, tp1_done=True, breakeven_done=True,
                        stop_level=3400.0, reversal_streak=1)
        from tests.test_reversal import reversing
        engine.apply(managed, evaluate(managed, reversing(3412.0), management(),
                                       ReversalConfig(action="close")))
        stages = [f.stage for f in store.fills_for(managed.deal_id)]
        self.assertIn("REVERSAL", stages)


class AggregationTests(unittest.TestCase):
    def test_wins_losses_and_totals_add_up(self):
        store = Store(":memory:")
        store.record_fill(fill("d1", "GOLD", "TP1", 3410.0, 0.5))
        store.record_fill(fill("d1", "GOLD", "TP2", 3420.0, 0.25))
        store.record_fill(fill("d2", "GOLD", "BROKER", 3390.0, 1.0, inferred=True))
        store.record_fill(fill("d3", "OIL_BRENT", "TP1", 3410.0, 1.0))

        report = journal_module.build(store, days=30)

        self.assertEqual(report.trades, 3)
        gold = next(s for s in report.epics if s.epic == "GOLD")
        self.assertEqual(gold.trades, 2)
        self.assertEqual(gold.wins, 1)
        self.assertEqual(gold.losses, 1)
        self.assertAlmostEqual(gold.total_r, 0.5 + 0.5 - 1.0, places=6)
        self.assertEqual(gold.hits("TP1"), 1)
        self.assertEqual(report.inferred, 1)

    def test_older_fills_fall_outside_the_window(self):
        store = Store(":memory:")
        store.record_fill(fill("old", ago_days=40))
        store.record_fill(fill("new", ago_days=1))
        self.assertEqual(journal_module.build(store, days=30).trades, 1)

    def test_best_and_worst_are_identified(self):
        store = Store(":memory:")
        store.record_fill(fill("d1", "GOLD", price=3420.0, fraction=1.0))
        store.record_fill(fill("d2", "OIL_BRENT", price=3380.0, fraction=1.0))
        report = journal_module.build(store, days=30)
        self.assertEqual(report.best.epic, "GOLD")
        self.assertEqual(report.worst.epic, "OIL_BRENT")

    def test_reversal_trades_are_compared_not_credited(self):
        # A counterfactual is unknowable, so the journal reports the two
        # populations side by side rather than claiming a saving.
        store = Store(":memory:")
        store.record_fill(fill("d1", price=3420.0, fraction=1.0))
        store.record_fill(fill("d2", price=3405.0, fraction=1.0))
        store.log_event("reversal", "DI cross", deal_id="d2", epic="GOLD")

        report = journal_module.build(store, days=30)
        self.assertEqual(len(report.with_reversal), 1)
        self.assertEqual(len(report.without_reversal), 1)

        text = journal_module.render(report)
        self.assertIn("vs 1 without", text)
        self.assertNotIn("saved", text.lower())


class RiskAtEntryTests(unittest.TestCase):
    """Risk is measured once. The stop moves; the yardstick must not."""

    def test_r_survives_the_stop_being_moved_to_break_even(self):
        managed = trade()
        before = managed.r_multiple(3412.0)
        managed.sl = managed.entry_price      # what break-even does
        self.assertAlmostEqual(managed.r_multiple(3412.0), before)
        self.assertEqual(managed.initial_risk, 10.0)

    def test_r_survives_a_trailing_stop_passing_the_entry(self):
        managed = trade()
        managed.sl = 3405.0                   # trailed into profit
        self.assertAlmostEqual(managed.r_multiple(3420.0), 2.0)

    def test_it_round_trips_through_the_database(self):
        from tmbot.store import Store
        store = Store(":memory:")
        managed = trade()
        managed.sl = managed.entry_price
        store.save_trade(managed)
        self.assertEqual(store.trade(managed.deal_id).initial_risk, 10.0)

    def test_an_old_record_without_it_still_reads(self):
        from tmbot.models import ManagedTrade
        raw = trade().to_dict()
        del raw["risk_at_entry"]
        self.assertEqual(ManagedTrade.from_dict(raw).initial_risk, 10.0)


class RenderTests(unittest.TestCase):
    def test_an_empty_journal_explains_itself(self):
        text = journal_module.render(journal_module.build(Store(":memory:"), 30))
        self.assertIn("No closed trades", text)

    def test_it_renders_in_arabic(self):
        store = Store(":memory:")
        store.record_fill(fill("d1", price=3420.0, fraction=1.0))
        text = journal_module.render(journal_module.build(store, 30), Translator("ar"))
        self.assertIn("الأداء", text)
        self.assertNotIn("trades", text)

    def test_estimated_exits_are_declared_rather_than_hidden(self):
        store = Store(":memory:")
        store.record_fill(fill("d1", price=3390.0, fraction=1.0, inferred=True))
        text = journal_module.render(journal_module.build(store, 30))
        self.assertIn("estimated", text)
        self.assertIn("fees", text)


if __name__ == "__main__":
    unittest.main()
