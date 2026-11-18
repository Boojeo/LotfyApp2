"""Tested odds, the entry range and the exit advice: the pure maths."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from tmbot.analysis import entry_range, exit_advice, odds
from tmbot.analysis.exit_advice import CLOSE, HOLD, MOVE_STOP, PARTIAL_CLOSE
from tmbot.config import ExitAdviceConfig, ReversalConfig
from tmbot.models import Candle, Direction, Stage, TrendStrength
from tests.helpers import BASE, RULES, candles, management, snapshot, trade

UTC = timezone.utc
NOW = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)


def bar(i, o, h, l, c):
    from datetime import timedelta as td
    return Candle(ts=BASE + td(minutes=15 * i), open=o, high=h, low=l, close=c)


def rising(n=400, step=1.0):
    return [bar(i, 100 + step * i, 100 + step * i + 0.5, 100 + step * i - 0.5,
                100 + step * i + 0.2) for i in range(n)]


def falling(n=400, step=1.0):
    return [bar(i, 1000 - step * i, 1000 - step * i + 0.5, 1000 - step * i - 0.5,
                1000 - step * i - 0.2) for i in range(n)]


def choppy(n=400):
    out = []
    for i in range(n):
        base = 100 + (3.0 if i % 2 else -3.0)
        out.append(bar(i, base, base + 1.0, base - 1.0, base))
    return out


class WilsonTests(unittest.TestCase):
    def test_the_interval_is_the_textbook_one(self):
        low, high = odds.wilson(50, 100)
        self.assertAlmostEqual(low, 0.4038, places=3)
        self.assertAlmostEqual(high, 0.5962, places=3)

    def test_it_narrows_with_more_samples_and_handles_the_extremes(self):
        wide = odds.wilson(5, 10)
        narrow = odds.wilson(500, 1000)
        self.assertGreater(wide[1] - wide[0], narrow[1] - narrow[0])
        self.assertEqual(odds.wilson(0, 0), None)
        low, high = odds.wilson(0, 40)
        self.assertEqual(low, 0.0)
        self.assertGreater(high, 0.0)
        self.assertLess(odds.wilson(40, 40)[0], 1.0)


class ReachTests(unittest.TestCase):
    def run_it(self, series, direction, stop=1.0, targets=None, **kw):
        kw.setdefault("horizon", 20)
        kw.setdefault("min_samples", 30)
        return odds.reach_rates(series, direction, stop, targets or {"TP1": 2.0}, **kw)

    def test_a_steady_rise_always_reaches_a_long_target_before_a_stop(self):
        result = self.run_it(rising(), Direction.BUY)
        reach = result.reach["TP1"]
        self.assertTrue(reach.enough)
        self.assertEqual((reach.hits, reach.stops), (reach.samples, 0))
        self.assertEqual(reach.p, 1.0)
        self.assertEqual(reach.q, 0.0)

    def test_the_same_rise_always_stops_a_short(self):
        reach = self.run_it(rising(), Direction.SELL).reach["TP1"]
        self.assertEqual(reach.p, 0.0)
        self.assertEqual(reach.q, 1.0)

    def test_a_falling_market_is_the_mirror_image(self):
        self.assertEqual(self.run_it(falling(), Direction.SELL).reach["TP1"].p, 1.0)
        self.assertEqual(self.run_it(falling(), Direction.BUY).reach["TP1"].p, 0.0)

    def test_a_farther_target_is_never_more_likely_than_a_nearer_one(self):
        result = self.run_it(choppy(), Direction.BUY, stop=2.0,
                             targets={"TP1": 1.0, "TP2": 2.0, "TP3": 3.0}, trend_filter=False)
        p = [result.reach[s].p for s in ("TP1", "TP2", "TP3")]
        self.assertTrue(p[0] >= p[1] >= p[2], p)

    def test_the_counts_always_add_up(self):
        result = self.run_it(choppy(), Direction.BUY, stop=2.0,
                             targets={"TP1": 1.0, "TP3": 4.0}, trend_filter=False)
        for reach in result.reach.values():
            self.assertEqual(reach.hits + reach.stops + reach.neither, reach.samples)

    def test_a_bar_touching_both_counts_as_the_stop(self):
        # every bar spans stop and target at once
        wide = [bar(i, 100, 110, 90, 100) for i in range(300)]
        reach = odds.reach_rates(wide, Direction.BUY, 0.3, {"TP1": 0.3},
                                 horizon=5, min_samples=30, trend_filter=False).reach["TP1"]
        self.assertEqual(reach.hits, 0)
        self.assertEqual(reach.stops, reach.samples)

    def test_only_fully_observed_test_trades_count(self):
        series = rising(300)
        result = self.run_it(series, Direction.BUY, horizon=50)
        first = max(odds.WARMUP, 15)
        self.assertLessEqual(result.samples, len(series) - 1 - 50 - first + 1)
        self.assertGreater(result.samples, 100)

    def test_too_little_history_gives_no_number(self):
        reach = self.run_it(rising(80), Direction.BUY, horizon=10).reach["TP1"]
        self.assertFalse(reach.enough)
        self.assertIsNone(reach.p)
        self.assertIsNone(reach.q)
        self.assertIsNone(reach.interval)
        self.assertIsNone(reach.to_dict()["p"])

    def test_the_trend_filter_is_used_only_when_there_is_enough_to_filter(self):
        up = self.run_it(rising(), Direction.BUY)
        self.assertTrue(up.conditioned)
        short = self.run_it(rising(), Direction.SELL)
        self.assertFalse(short.conditioned, "no downtrend samples: falls back to all entries")
        self.assertGreater(short.samples, up.samples - 1000)

    def test_bad_geometry_is_skipped_not_guessed(self):
        self.assertEqual(self.run_it(rising(), Direction.BUY, stop=0).reach, {})
        self.assertEqual(self.run_it(rising(), Direction.BUY, targets={"TP1": 0}).reach, {})
        self.assertEqual(self.run_it(rising(), Direction.BUY, horizon=0).reach, {})

    def test_the_result_serialises_with_its_sample_size(self):
        data = self.run_it(rising(), Direction.BUY).to_dict()
        self.assertEqual(data["reach"]["TP1"]["samples"], data["samples"])
        self.assertIn("low", data["reach"]["TP1"])
        self.assertGreater(data["bars"], 300)


def gold_range(**overrides):
    base = dict(
        direction_sign=1, reference=3400.0, stop=3397.0, targets=[3404.1, 3407.3, 3412.1],
        atr=4.0, spread=0.2, slippage=0.0, commission_per_lot=0.0, value_per_point=100.0,
        sizing={"per_leg": 0.1, "legs": 3, "max_money": 100.0, "risk_percent": 1.0,
                "currency": "USD"},
        deal_targets=(1, 2, 3), min_reward_risk=0.8, min_stop_atr=0.6,
        created_at=NOW, valid_hours=4.0, bias="BULLISH",
    )
    base.update(overrides)
    return entry_range.build(**base)


class EntryRangeTests(unittest.TestCase):
    def test_the_bounds_come_from_the_existing_rules(self):
        r = gold_range()
        self.assertTrue(r["valid"])
        self.assertEqual(r["limited_by"], "risk")
        self.assertAlmostEqual(r["high"], 3397.0 + (100.0 / 30.0 - 0.1), places=4)   # risk limit
        self.assertAlmostEqual(r["low"], 3397.0 + 0.6 * 4.0 + 0.1, places=4)         # min stop
        self.assertEqual(r["preferred"], 3400.0)
        self.assertTrue(r["low"] <= r["preferred"] <= r["high"])

    def test_the_worst_entry_loses_exactly_the_risk_limit_and_no_more(self):
        r = gold_range()
        worst = max(r["rows"], key=lambda row: row["entry"])
        self.assertAlmostEqual(worst["loss"], 100.0, places=1)
        self.assertTrue(all(row["within_limit"] for row in r["rows"]))
        self.assertLess(r["rows"][0]["loss"], r["rows"][-1]["loss"])

    def test_the_lots_are_never_changed_by_the_entry(self):
        r = gold_range()
        self.assertEqual((r["per_leg"], r["legs"]), (0.1, 3))

    def test_money_at_the_preferred_entry_matches_the_sizing_rule(self):
        row = next(x for x in gold_range()["rows"] if x["is_preferred"])
        self.assertAlmostEqual(row["loss"], (3.0 + 0.1) * 100 * 0.3, places=2)      # 93.00
        self.assertEqual(len(row["profits"]), 3)
        self.assertAlmostEqual(row["profits"][0], round((4.1 - 0.1) * 100 * 0.1, 2))
        self.assertAlmostEqual(row["profit_total"], sum(row["profits"]), places=2)
        self.assertAlmostEqual(row["rr_basket"], row["profit_total"] / row["loss"], places=2)

    def test_a_better_entry_earns_more_and_risks_less(self):
        rows = gold_range()["rows"]
        self.assertGreater(rows[0]["profit_total"], rows[-1]["profit_total"])
        self.assertGreater(rows[0]["rr"][0], rows[-1]["rr"][0])
        self.assertGreater(rows[0]["rr_basket"], rows[-1]["rr_basket"])

    def test_reward_risk_per_target_uses_costs(self):
        row = next(x for x in gold_range()["rows"] if x["is_preferred"])
        self.assertAlmostEqual(row["rr"][0], (4.1 - 0.1) / (3.0 + 0.1), places=3)

    def test_commission_and_slippage_narrow_the_range_and_lower_the_profit(self):
        sizing = {"per_leg": 0.09, "legs": 3, "max_money": 100.0, "risk_percent": 1.0,
                  "currency": "USD"}      # smaller lots, as a plan with these costs would advise
        plain = gold_range(sizing=sizing)
        costly = gold_range(sizing=sizing, commission_per_lot=7.0, slippage=0.2)
        self.assertLess(costly["high"], plain["high"])
        plain_row = next(x for x in plain["rows"] if x["is_preferred"])
        costly_row = next(x for x in costly["rows"] if x["is_preferred"])
        self.assertGreater(costly_row["loss"], plain_row["loss"])
        self.assertLess(costly_row["profit_total"], plain_row["profit_total"])

    def test_without_lots_only_reward_risk_limits_it_and_no_money_is_shown(self):
        r = gold_range(sizing={}, value_per_point=None)
        self.assertTrue(r["valid"])
        self.assertEqual(r["limited_by"], "reward_risk")
        self.assertFalse(r["have_lots"])
        self.assertNotIn("loss", r["rows"][0])
        self.assertIn("rr", r["rows"][0])

    def test_a_short_is_the_mirror_image(self):
        r = gold_range(direction_sign=-1, reference=3400.0, stop=3403.0,
                       targets=[3395.9, 3392.7, 3387.9])
        self.assertTrue(r["valid"])
        self.assertLess(r["low"], r["preferred"])
        self.assertGreater(r["high"], r["preferred"])
        self.assertAlmostEqual(r["low"], 3403.0 - (100.0 / 30.0 - 0.1), places=4)
        worst = min(r["rows"], key=lambda row: row["entry"])
        self.assertAlmostEqual(worst["loss"], 100.0, places=1)

    def test_a_reference_that_already_breaks_the_rules_gives_no_range(self):
        r = gold_range(sizing={"per_leg": 0.2, "legs": 3, "max_money": 100.0,
                               "risk_percent": 1.0, "currency": "USD"})
        self.assertFalse(r["valid"])
        self.assertEqual(r["reason"], "no_acceptable_entry")
        self.assertEqual(r["rows"], [])

    def test_the_plain_dict_survives_json(self):
        import json
        r = gold_range()
        self.assertEqual(json.loads(json.dumps(r))["preferred"], 3400.0)


class EntryCheckTests(unittest.TestCase):
    def check(self, r=None, **kw):
        kw.setdefault("price", 3400.0)
        kw.setdefault("spread", 0.2)
        kw.setdefault("now", NOW + timedelta(minutes=5))
        return entry_range.check(r or gold_range(), **kw)

    def keys(self, **kw):
        return [k for k, _ in self.check(**kw)[1]]

    def test_inside_the_range_it_is_valid(self):
        valid, reasons = self.check()
        self.assertTrue(valid, reasons)

    def test_every_edge_of_the_range_is_valid_and_just_past_it_is_not(self):
        r = gold_range()
        self.assertTrue(self.check(r, price=r["low"] + 0.001)[0])
        self.assertTrue(self.check(r, price=r["high"] - 0.001)[0])
        self.assertIn("outside_range", self.keys(r=r, price=r["high"] + 0.2))
        self.assertIn("outside_range", self.keys(r=r, price=r["low"] - 0.2))

    def test_a_wider_spread_can_break_the_risk_limit_at_the_edge(self):
        r = gold_range()
        edge = r["high"] - 0.001
        self.assertTrue(self.check(r, price=edge)[0])
        self.assertIn("risk_over_limit", self.keys(r=r, price=edge, spread=1.5))

    def test_tp1_passed_and_stop_touched(self):
        self.assertIn("tp1_passed", self.keys(price=3404.2))
        self.assertIn("stop_touched", self.keys(price=3396.9))

    def test_the_range_expires_after_its_refresh_interval(self):
        self.assertIn("expired", self.keys(now=NOW + timedelta(hours=4, minutes=1)))
        self.assertNotIn("expired", self.keys(now=NOW + timedelta(hours=3, minutes=59)))

    def test_a_refresh_that_is_switched_off_still_expires_it(self):
        r = gold_range(valid_hours=0.0)
        self.assertIn("expired", self.keys(r=r, now=NOW + timedelta(hours=5)))

    def test_a_newer_plan_pointing_the_other_way_invalidates_it(self):
        self.assertIn("bias_changed", self.keys(latest_bias="BEARISH"))
        self.assertNotIn("bias_changed", self.keys(latest_bias="BULLISH"))
        self.assertNotIn("bias_changed", self.keys(latest_bias=None))

    def test_an_empty_range_is_never_valid(self):
        self.assertFalse(entry_range.check({}, price=1, spread=0, now=NOW)[0])
        bad = gold_range(sizing={"per_leg": 0.2, "legs": 3, "max_money": 100.0,
                                 "risk_percent": 1.0})
        self.assertFalse(entry_range.check(bad, price=3400, spread=0, now=NOW)[0])


def view_trade(**kw):
    return trade(Direction.BUY, entry=3400.0, tp1=3410.0, tp2=3420.0, tp3=3430.0,
                 sl=3390.0, **kw)


def make_odds(p, q, *, stages=("TP1", "TP2", "TP3"), samples=200):
    reach = {}
    for stage in stages:
        hits = round(p[stage] * samples)
        stops = round(q[stage] * samples)
        reach[stage] = odds.Reach(stage, 1.0, samples, hits, stops, samples - hits - stops, True)
    return odds.OddsResult(reach=reach, samples=samples, conditioned=True, bars=1000, horizon=96)


class AdviceTests(unittest.TestCase):
    cfg = management(exit_model="three_deals")
    rev = ReversalConfig()
    adv = ExitAdviceConfig()

    def advise(self, t, price, odds_result, *, signal=None, snap=None, config=None):
        snap = snap or snapshot(price, atr=4.0, strength=TrendStrength.MODERATE)
        return exit_advice.advise(t, snap, odds_result, config or self.cfg, self.rev,
                                  self.adv, signal=signal)

    def runner(self, **kw):
        return view_trade(leg_target=Stage.TP3, leg_index=2, basket_size=3, **kw)

    def test_the_candidates_are_the_targets_still_ahead(self):
        t = self.runner()
        self.assertEqual(exit_advice.candidate_stages(t, 3415.0, self.cfg), ["TP2", "TP3"])
        leg = view_trade(leg_target=Stage.TP2, leg_index=1, basket_size=3)
        self.assertEqual(exit_advice.candidate_stages(leg, 3401.0, self.cfg), ["TP2"])
        self.assertEqual(exit_advice.candidate_stages(leg, 3421.0, self.cfg), [])

    def test_a_partial_close_deal_skips_rungs_already_done(self):
        t = view_trade(tp1_done=True)
        cfg = management()
        self.assertEqual(exit_advice.candidate_stages(t, 3401.0, cfg), ["TP2", "TP3"])

    def test_the_geometry_is_in_atrs_from_the_current_price(self):
        t = self.runner()
        stop_atr, targets = exit_advice.geometry(t, snapshot(3415.0, atr=5.0), self.cfg)
        self.assertAlmostEqual(stop_atr, (3415.0 - 3390.0) / 5.0)
        self.assertAlmostEqual(targets["TP3"], 3.0)
        self.assertNotIn("TP1", targets)

    def test_the_realistic_target_is_the_best_value_not_automatically_tp3(self):
        # TP1 and TP2 are done; TP3 mostly reverses first
        t = self.runner(tp1_done=True, tp2_done=True)
        t.stop_level = t.sl = 3400.0
        result = make_odds({"TP3": 0.15}, {"TP3": 0.70}, stages=("TP3",))
        a = self.advise(t, 3422.0, result)
        self.assertEqual(a.best_stage, "TP3")
        view = a.targets[0]
        # EV = .15*3.0 + .70*0 + .15*2.2 = 0.78 < 2.2 in hand
        self.assertAlmostEqual(view.ev_r, 0.15 * 3.0 + 0.70 * 0.0 + 0.15 * 2.2, places=2)
        self.assertEqual(a.action, CLOSE)
        self.assertEqual(a.reasons[0][0], "close_ev")
        self.assertIn("samples", a.reasons[0][1])

    def test_a_target_that_is_likely_enough_is_held(self):
        t = self.runner()
        t.stop_level = t.sl = 3400.0
        result = make_odds({"TP1": 0.9, "TP2": 0.8, "TP3": 0.7},
                           {"TP1": 0.05, "TP2": 0.1, "TP3": 0.2})
        a = self.advise(t, 3402.0, result)
        self.assertEqual(a.action, HOLD)
        self.assertEqual(a.best_stage, "TP3")

    def test_when_a_nearer_target_has_the_better_value_the_advice_names_it(self):
        t = self.runner()
        t.stop_level = t.sl = 3400.0
        result = make_odds({"TP1": 0.9, "TP2": 0.8, "TP3": 0.1},
                           {"TP1": 0.05, "TP2": 0.15, "TP3": 0.85})
        a = self.advise(t, 3401.0, result)
        self.assertEqual(a.best_stage, "TP2")
        self.assertEqual(a.action, HOLD)
        self.assertIn("realistic_target", [k for k, _ in a.reasons])

    def test_a_losing_deal_is_not_closed_by_the_odds_the_stop_is_its_protection(self):
        t = self.runner()
        result = make_odds({"TP1": 0.01, "TP2": 0.0, "TP3": 0.0},
                           {"TP1": 0.9, "TP2": 0.95, "TP3": 0.95})
        a = self.advise(t, 3396.0, result)
        self.assertLess(a.r_now, 0)
        self.assertEqual(a.action, HOLD)

    def test_without_enough_history_there_is_no_number_and_it_says_so(self):
        t = self.runner()
        thin = odds.OddsResult(reach={"TP3": odds.Reach("TP3", 2.0, 5, 3, 1, 1, False)},
                               samples=5, bars=100, horizon=96)
        a = self.advise(t, 3415.0, thin)
        self.assertFalse(a.tested)
        self.assertIsNone(a.targets[-1].ev_r)
        self.assertIsNone(a.targets[-1].p)
        self.assertIn("no_odds", [k for k, _ in a.reasons])
        self.assertEqual(a.action, HOLD)
        none = self.advise(t, 3415.0, None)
        self.assertFalse(none.tested)

    def test_a_confirmed_reversal_in_profit_closes_with_the_evidence(self):
        from tmbot.analysis.reversal import ReversalSignal, Signal
        t = self.runner()
        signal = ReversalSignal(detected=True, against=Direction.SELL, agreeing=3, required=2,
                                confidence=80.0, signals=[Signal("a", True, "x")],
                                confirmed=True, streak=3)
        a = self.advise(t, 3412.0, None, signal=signal)
        self.assertEqual(a.action, CLOSE)
        self.assertEqual(a.reasons[0][0], "close_reversal")
        self.assertEqual(a.reasons[0][1]["agreeing"], 3)

    def test_a_muted_reversal_is_respected(self):
        from tmbot.analysis.reversal import ReversalSignal, Signal
        t = self.runner(reversal_muted=True)
        signal = ReversalSignal(detected=True, against=Direction.SELL, agreeing=3, required=2,
                                confidence=80.0, signals=[Signal("a", True, "x")],
                                confirmed=True, streak=3)
        self.assertEqual(self.advise(t, 3412.0, None, signal=signal).action, HOLD)

    def test_fading_momentum_with_a_reversal_signal_moves_the_stop_up(self):
        from tmbot.analysis.reversal import ReversalSignal, Signal
        t = self.runner()
        snap = snapshot(3412.0, atr=4.0)
        snap = replace(snap, macd_hist=0.4, macd_hist_prev=0.9)
        signal = ReversalSignal(detected=False, against=Direction.SELL, agreeing=1, required=2,
                                confidence=0.0, signals=[Signal("a", True, "x")])
        a = self.advise(t, 3412.0, None, signal=signal, snap=snap)
        self.assertEqual(a.action, MOVE_STOP)
        self.assertAlmostEqual(a.stop_level, 3412.0 - self.rev.tighten_atr * 4.0)
        self.assertGreater(a.stop_level, t.stop_level)
        self.assertEqual(a.reasons[0][0], "move_stop")

    def test_fading_momentum_without_a_reversal_signal_just_holds(self):
        from tmbot.analysis.reversal import ReversalSignal
        t = self.runner()
        snap = replace(snapshot(3412.0, atr=4.0), macd_hist=0.4, macd_hist_prev=0.9)
        signal = ReversalSignal(detected=False, against=Direction.SELL, agreeing=0, required=2,
                                confidence=0.0, signals=[])
        self.assertEqual(self.advise(t, 3412.0, None, signal=signal, snap=snap).action, HOLD)

    def test_a_stop_that_would_not_improve_is_not_recommended(self):
        from tmbot.analysis.reversal import ReversalSignal, Signal
        t = self.runner()
        t.stop_level = t.sl = 3411.0           # already tighter than price - 1 ATR
        snap = replace(snapshot(3412.0, atr=4.0), macd_hist=0.4, macd_hist_prev=0.9)
        signal = ReversalSignal(detected=False, against=Direction.SELL, agreeing=1, required=2,
                                confidence=0.0, signals=[Signal("a", True, "x")])
        a = self.advise(t, 3412.0, None, signal=signal, snap=snap)
        self.assertEqual(a.action, HOLD)
        self.assertIn("stop_not_improved", [k for k, _ in a.reasons])

    def test_a_short_deal_mirrors_all_of_it(self):
        t = trade(Direction.SELL, entry=3400.0, tp1=3390.0, tp2=3380.0, tp3=3370.0, sl=3410.0,
                  leg_target=Stage.TP3, leg_index=2, basket_size=3)
        t.stop_level = t.sl = 3400.0
        result = make_odds({"TP3": 0.1}, {"TP3": 0.8}, stages=("TP3",))
        snap = snapshot(3378.0, atr=4.0)
        a = exit_advice.advise(t, snap, result, self.cfg, self.rev, self.adv)
        self.assertEqual(a.action, CLOSE)
        self.assertGreater(a.r_now, 2.0)

    def test_the_evidence_is_always_listed(self):
        a = self.advise(self.runner(), 3402.0, None)
        keys = [k for k, _ in a.reasons]
        self.assertIn("evidence", keys)
        self.assertIn("adx", a.evidence)
        self.assertIn("reversal_agreeing", a.evidence)

    def test_partial_close_only_in_the_partial_close_model_and_never_below_the_minimum(self):
        cfg = management()
        t = view_trade(size=1.0, tp1_done=True, tp2_done=True)
        t.stop_level = t.sl = 3400.0
        result = make_odds({"TP3": 0.1}, {"TP3": 0.8}, stages=("TP3",))
        a = exit_advice.advise(t, snapshot(3422.0, atr=4.0), result, cfg, self.rev, self.adv)
        self.assertEqual(a.action, PARTIAL_CLOSE)
        self.assertAlmostEqual(a.size, 0.5)
        tiny = view_trade(size=0.01, tp1_done=True, tp2_done=True)
        tiny.stop_level = tiny.sl = 3400.0
        a = exit_advice.advise(tiny, snapshot(3422.0, atr=4.0), result, cfg, self.rev, self.adv)
        self.assertEqual(a.action, CLOSE, "0.005 is below the minimum: close the whole deal")

    def test_the_advice_serialises(self):
        import json
        a = self.advise(self.runner(), 3402.0, make_odds(
            {"TP1": 0.9, "TP2": 0.8, "TP3": 0.7}, {"TP1": 0.05, "TP2": 0.1, "TP3": 0.2}))
        data = json.loads(json.dumps(a.to_dict()))
        self.assertEqual(data["action"], a.action)
        self.assertEqual(data["targets"][0]["stage"], "TP1")


if __name__ == "__main__":
    unittest.main()


class PlanIntegrationTests(unittest.TestCase):
    """The range and the odds are part of every plan, and never cost you the plan."""

    def build(self, **config_changes):
        from tests.test_style import UP, auto_config, broker_with
        from tmbot.analysis.report import ReportBuilder
        config = auto_config()
        for dotted, value in config_changes.items():
            section, field = dotted.split("__")
            setattr(getattr(config, section), field, value)
        broker = broker_with(M15=UP, H1=UP)
        return ReportBuilder(broker, config).build("GOLD"), broker

    def test_every_plan_carries_a_range_and_tested_odds(self):
        plan, _ = self.build()
        self.assertTrue(plan.entry_zone["valid"])
        self.assertEqual(plan.entry_zone["preferred"], plan.reference_price)
        self.assertEqual(plan.entry_zone["stop"], plan.sl)
        self.assertEqual(plan.entry_zone["targets"], plan.targets)
        self.assertEqual(set(plan.odds["reach"]), {"TP1", "TP2", "TP3"})
        self.assertEqual(plan.odds["timeframe"], plan.style["entry"])
        self.assertGreater(plan.odds["samples"], 30)

    def test_the_range_uses_the_plans_own_lots_and_never_changes_them(self):
        plan, _ = self.build()
        z = plan.entry_zone
        self.assertEqual((z["legs"], z["per_leg"]), (plan.sizing["legs"], plan.sizing["per_leg"]))
        self.assertEqual(z["max_money"], plan.sizing["max_money"])
        self.assertTrue(all(row["within_limit"] for row in z["rows"]))

    def test_the_odds_test_the_plans_own_distances(self):
        plan, _ = self.build()
        reach = plan.odds["reach"]
        self.assertAlmostEqual(plan.odds["stop_atr"], plan.risk / plan.atr, places=2)
        self.assertAlmostEqual(reach["TP1"]["distance_atr"],
                               (plan.tp1 - plan.reference_price) / plan.atr, places=2)
        self.assertTrue(reach["TP1"]["distance_atr"] < reach["TP2"]["distance_atr"]
                        < reach["TP3"]["distance_atr"])

    def test_both_survive_the_store_and_old_plans_still_load_and_render(self):
        from tmbot.analysis.report import render_full, render_text
        from tmbot.models import TradePlan
        plan, _ = self.build()
        again = TradePlan.from_dict(json.loads(json.dumps(plan.to_dict())))
        self.assertEqual(again.entry_zone["rows"], plan.entry_zone["rows"])
        self.assertEqual(again.odds["reach"], plan.odds["reach"])
        self.assertEqual(again.rebase(plan.reference_price + 1).entry_zone, plan.entry_zone)
        raw = plan.to_dict()
        raw.pop("entry_zone")
        raw.pop("odds")
        old = TradePlan.from_dict(raw)
        self.assertEqual((old.entry_zone, old.odds), ({}, {}))
        self.assertNotIn("Entry Range", render_full(old))
        self.assertNotIn("Tested odds", render_full(old))
        self.assertNotIn("🧪", render_text(old))

    def test_the_full_report_shows_the_range_the_table_the_rules_and_the_odds(self):
        from tmbot.analysis.report import render_full
        plan, _ = self.build()
        text = render_full(plan)
        self.assertIn("**Entry Range:**", text)
        self.assertIn("preferred", text)
        self.assertIn("loss at SL -", text)
        self.assertIn("R:R", text)
        self.assertIn("no longer valid when", text)
        self.assertIn("Lots are the plan's own", text)
        self.assertIn("**Tested odds** (reached before the stop)", text)
        self.assertIn("measured frequencies", text)
        self.assertIn("not a forecast", text)
        self.assertEqual(text.count("\n---\n"), 4, "the layout of the report is unchanged")

    def test_the_short_message_shows_a_line_for_each(self):
        from tmbot.analysis.report import render_text
        plan, _ = self.build()
        text = render_text(plan)
        self.assertIn("🎯 Entry ", text)
        self.assertIn("(prefer ", text)
        self.assertIn("🧪 Tested odds: TP1 ", text)

    def test_arabic_renders_without_missing_wording(self):
        from tmbot.analysis.report import render_full, render_text
        from tmbot.i18n import Translator
        plan, _ = self.build()
        for text in (render_full(plan, Translator("ar")), render_text(plan, Translator("ar"))):
            for marker in ("[ez.", "[sm.", "[tr."):
                self.assertNotIn(marker, text)

    def test_a_failure_in_either_never_costs_you_the_plan(self):
        from unittest import mock
        from tmbot.analysis import entry_range as er
        from tmbot.analysis import odds as od
        with mock.patch.object(er, "build", side_effect=RuntimeError("boom")), \
             mock.patch.object(od, "reach_rates", side_effect=RuntimeError("boom")):
            plan, _ = self.build()
        self.assertEqual((plan.entry_zone, plan.odds), ({}, {}))
        self.assertGreater(plan.reference_price, 0)
        self.assertEqual(len(plan.targets), 3)

    def test_switching_the_odds_off_leaves_only_the_range(self):
        plan, _ = self.build(exit_advice__enabled=False)
        self.assertEqual(plan.odds, {})
        self.assertTrue(plan.entry_zone["valid"])

    def test_too_little_history_shows_no_number_and_says_why(self):
        from tmbot.analysis.report import render_full, render_text
        from tests.test_style import UP, auto_config, broker_with
        from tmbot.analysis.report import ReportBuilder
        broker = broker_with(M15=UP, H1=UP)
        for tf in ("M15", "H1"):
            broker.set_candles("GOLD", tf, broker.candles("GOLD", tf, 400)[-90:])
        plan = ReportBuilder(broker, auto_config()).build("GOLD")
        self.assertFalse(all(r["enough"] for r in plan.odds["reach"].values()))
        full = render_full(plan)
        self.assertIn("not enough history", full)
        self.assertNotIn("**Tested odds** (reached", full)
        self.assertIn("🧪 Tested odds: not enough history", render_text(plan))

    def test_the_long_history_is_asked_for_and_a_failure_falls_back(self):
        from unittest import mock
        from tests.test_style import UP, auto_config, broker_with
        from tmbot.analysis.report import ReportBuilder
        broker = broker_with(M15=UP, H1=UP)
        real = broker.candles
        seen = []

        def spy(epic, timeframe, limit):
            seen.append(limit)
            return real(epic, timeframe, limit)
        broker.candles = spy
        ReportBuilder(broker, auto_config()).build("GOLD")
        self.assertIn(1000, seen)

        def flaky(epic, timeframe, limit):
            if limit == 1000:
                raise RuntimeError("rate limited")
            return real(epic, timeframe, limit)
        broker.candles = flaky
        plan = ReportBuilder(broker, auto_config()).build("GOLD")
        self.assertGreater(plan.odds["samples"], 30, "fell back to the history already fetched")

    def test_the_odds_and_range_do_not_change_what_was_already_in_the_plan(self):
        """The plan's own numbers are produced before either is computed."""
        plan, _ = self.build()
        again, _ = self.build(exit_advice__enabled=False)
        for field in ("reference_price", "tp1", "tp2", "tp3", "sl", "atr", "confidence"):
            self.assertEqual(getattr(plan, field), getattr(again, field), field)
        self.assertEqual(plan.sizing, again.sizing)
        self.assertEqual(plan.assessment, again.assessment)

