"""Trend-reversal failsafe: detection, confirmation, and the limits on acting."""

from __future__ import annotations

import unittest

from tmbot.analysis.reversal import detect, recovery_stop
from tmbot.config import ReversalConfig
from tmbot.manage.rules import DecisionKind, evaluate
from tmbot.models import Direction, TrendStrength
from tests.helpers import management, snapshot, trade


def reversing(price: float = 3405.0, **overrides):
    """A snapshot where the trend has turned against a long."""
    base = dict(
        atr=4.0, strength=TrendStrength.STRONG, swing_low=3408.0,
        plus_di=18.0, minus_di=28.0, plus_di_prev=27.0, minus_di_prev=19.0,
        ema_fast=3402.0, ema_slow=3404.0, ema_fast_prev=3406.0, ema_slow_prev=3405.0,
        macd_hist=-1.4, macd_hist_prev=0.3,
    )
    base.update(overrides)
    snap = snapshot(price, **{k: v for k, v in base.items()
                              if k in ("atr", "strength", "swing_low", "swing_high")})
    # MarketSnapshot is frozen; rebuild with the cross inputs.
    from dataclasses import replace
    from tmbot.models import Candle
    from datetime import datetime, timezone
    snap = replace(snap, candles=[Candle(
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=price, high=price, low=price, close=price,
    )], **{k: v for k, v in base.items() if k not in
           ("atr", "strength", "swing_low", "swing_high")})
    return snap


def quiet(price: float = 3405.0):
    """Nothing wrong: trend intact, no crosses."""
    from dataclasses import replace
    from tmbot.models import Candle
    from datetime import datetime, timezone
    snap = snapshot(price, atr=4.0, strength=TrendStrength.STRONG, swing_low=3390.0)
    return replace(
        snap,
        candles=[Candle(ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        open=price, high=price, low=price, close=price)],
        plus_di=28.0, minus_di=15.0, plus_di_prev=27.0, minus_di_prev=16.0,
        ema_fast=3406.0, ema_slow=3402.0, ema_fast_prev=3405.0, ema_slow_prev=3402.0,
        macd_hist=0.8, macd_hist_prev=0.5,
    )


class DetectionTests(unittest.TestCase):
    def test_a_healthy_trend_raises_nothing(self):
        signal = detect(trade(), quiet(), ReversalConfig())
        self.assertFalse(signal.detected)
        self.assertEqual(signal.agreeing, 0)

    def test_agreeing_signals_are_detected_and_named(self):
        signal = detect(trade(), reversing(), ReversalConfig())
        self.assertTrue(signal.detected)
        self.assertGreaterEqual(signal.agreeing, 2)
        names = {s.name for s in signal.fired}
        self.assertIn("di_cross", names)
        self.assertIn("structure", names)
        self.assertIn("-DI crossed +DI", signal.summary())

    def test_one_signal_alone_is_not_enough(self):
        # Structure break only -- no DI cross, no EMA cross, no momentum flip.
        snap = quiet()
        from dataclasses import replace
        snap = replace(snap, swing_low=3408.0)
        signal = detect(trade(), snap, ReversalConfig(min_signals=2))
        self.assertFalse(signal.detected, "a lone wick through a swing low is noise")

    def test_a_directionless_market_has_no_trend_to_reverse(self):
        from dataclasses import replace
        snap = replace(reversing(), adx=11.0)
        self.assertFalse(detect(trade(), snap, ReversalConfig(adx_min=20)).detected)

    def test_confirmation_needs_consecutive_cycles(self):
        config = ReversalConfig(confirm_cycles=2)
        first = detect(trade(reversal_streak=0), reversing(), config)
        self.assertTrue(first.detected)
        self.assertFalse(first.confirmed, "one cycle is not confirmation")

        second = detect(trade(reversal_streak=1), reversing(), config)
        self.assertTrue(second.confirmed)

    def test_the_streak_resets_when_the_signal_goes_away(self):
        self.assertEqual(detect(trade(reversal_streak=3), quiet(), ReversalConfig()).streak, 0)

    def test_a_short_reversal_mirrors_a_long_one(self):
        from dataclasses import replace
        snap = replace(
            reversing(price=3395.0), swing_low=None, swing_high=3392.0,
            plus_di=28.0, minus_di=18.0, plus_di_prev=19.0, minus_di_prev=27.0,
            ema_fast=3398.0, ema_slow=3396.0, ema_fast_prev=3394.0, ema_slow_prev=3395.0,
            macd_hist=1.4, macd_hist_prev=-0.3,
        )
        signal = detect(trade(Direction.SELL), snap, ReversalConfig())
        self.assertTrue(signal.detected)
        self.assertIs(signal.against, Direction.BUY)


class ActionTests(unittest.TestCase):
    def confirmed(self):
        return trade(tp1_done=True, breakeven_done=True, stop_level=3400.0,
                     remaining_size=0.5, reversal_streak=1)

    def test_it_tightens_the_stop_and_never_opens_anything(self):
        result = evaluate(self.confirmed(), reversing(3412.0), management(),
                          ReversalConfig(action="tighten", tighten_atr=1.0))
        stop = next(d for d in result.decisions if d.kind is DecisionKind.SET_STOP)
        self.assertGreater(stop.stop_level, 3400.0, "must tighten, not loosen")
        self.assertLess(stop.stop_level, 3412.0)
        self.assertIn("reversal", stop.key)
        # The founding rule: nothing here can create exposure.
        self.assertNotIn(DecisionKind.CLOSE_ALL,
                         [d.kind for d in result.decisions if d.size is None])

    def test_close_action_exits_the_whole_position(self):
        result = evaluate(self.confirmed(), reversing(3412.0), management(),
                          ReversalConfig(action="close"))
        close = next(d for d in result.decisions if d.kind is DecisionKind.CLOSE_ALL)
        self.assertEqual(close.size, 0.5)

    def test_alert_action_touches_nothing(self):
        result = evaluate(self.confirmed(), reversing(3412.0), management(),
                          ReversalConfig(action="alert"))
        self.assertTrue(result.reversal.confirmed)
        self.assertFalse(any("reversal" in d.key for d in result.decisions))

    def test_a_tightened_stop_still_obeys_the_ratchet(self):
        # Stop already at 3411; a reversal level below that must be ignored.
        managed = trade(tp1_done=True, breakeven_done=True, stop_level=3411.0,
                        remaining_size=0.5, reversal_streak=1)
        result = evaluate(managed, reversing(3412.0), management(),
                          ReversalConfig(action="tighten", tighten_atr=3.0))
        stops = [d for d in result.decisions if d.kind is DecisionKind.SET_STOP]
        self.assertEqual(stops, [], "a looser stop is never sent")

    def test_an_unconfirmed_reversal_does_not_act(self):
        managed = trade(tp1_done=True, breakeven_done=True, stop_level=3400.0,
                        remaining_size=0.5, reversal_streak=0)
        result = evaluate(managed, reversing(3412.0), management(),
                          ReversalConfig(confirm_cycles=2, action="tighten"))
        self.assertFalse(any("reversal" in d.key for d in result.decisions))

    def test_hold_mutes_the_failsafe_for_that_trade(self):
        managed = self.confirmed()
        managed.reversal_muted = True
        result = evaluate(managed, reversing(3412.0), management(),
                          ReversalConfig(action="tighten"))
        self.assertFalse(any("reversal" in d.key for d in result.decisions))

    def test_a_losing_trade_is_reported_rather_than_acted_on(self):
        # Below min_r_to_act there is nothing safe left to do without either
        # widening risk or opening a position, and both are forbidden.
        managed = trade(remaining_size=1.0, reversal_streak=1, stop_level=3390.0)
        result = evaluate(managed, reversing(3396.0), management(),
                          ReversalConfig(action="tighten", min_r_to_act=0.5))
        self.assertFalse(any("reversal" in d.key for d in result.decisions))
        self.assertTrue(any("alerting instead" in b.reason for b in result.blocked))

    def test_disabled_means_no_detection_at_all(self):
        result = evaluate(self.confirmed(), reversing(3412.0), management(),
                          ReversalConfig(enabled=False))
        self.assertIsNone(result.reversal)


class RecoveryStopTests(unittest.TestCase):
    def test_the_recovery_level_sits_one_atr_behind_price(self):
        level = recovery_stop(trade(), reversing(3412.0), ReversalConfig(tighten_atr=1.0))
        self.assertAlmostEqual(level, 3412.0 - 4.0, places=6)

    def test_it_declines_without_volatility_to_measure(self):
        from dataclasses import replace
        self.assertIsNone(
            recovery_stop(trade(), replace(reversing(), atr=0.0), ReversalConfig())
        )


if __name__ == "__main__":
    unittest.main()
