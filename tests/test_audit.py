"""The rejection audit: /why, the replay, the cross-check, the sizing invariants,
and tests that pin down what the code does today (each one backs a finding)."""

from __future__ import annotations

import random
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from tmbot.analysis import funnel, replay, risk as risk_module
from tmbot.analysis import quality as quality_module
from tmbot.analysis import entry_range
from tmbot.analysis.report import ReportBuilder, render_full, render_text
from tmbot.broker.base import BrokerAdapter
from tmbot.config import Config
from tmbot.i18n import CATALOG, Translator
from tmbot.models import Bias, Candle, Direction, TradePlan
from tests.helpers import BASE, RULES
from tests.test_auto import Harness as AutoHarness, approved_plan
from tests.test_style import GOLD, auto_config, broker_with
from tests.test_supervisor import build_supervisor, texts

UTC = timezone.utc
T = Translator()


# ---------------------------------------------------------------------- funnel

def plan_with(**kw) -> TradePlan:
    plan = approved_plan()
    for key, value in kw.items():
        setattr(plan, key, value)
    return plan


class FunnelTests(unittest.TestCase):
    def blocker(self, plan, minimum=60.0):
        return funnel.auto_blocker(plan, min_strength=minimum, t=T)

    def test_a_clean_plan_passes_every_plan_level_check(self):
        self.assertEqual(self.blocker(plan_with()), funnel.PASS)

    def test_each_check_is_named_in_the_order_the_code_runs_them(self):
        cases = [
            (dict(bias=Bias.NEUTRAL), "no_direction"),
            (dict(advisory_only=True), "no_direction"),
            (dict(direction=Direction.SELL), "against_bias"),
            (dict(confidence=59.0), "weak_signal"),
        ]
        for changes, expected in cases:
            self.assertEqual(self.blocker(plan_with(**changes)), expected, changes)
        plan = plan_with()
        plan.assessment = {**plan.assessment, "verdict": "CAUTION"}
        self.assertEqual(self.blocker(plan), "not_approved:CAUTION")
        for state in ("CONFLICT", "PARTIAL", "UNCERTAIN"):
            plan = plan_with()
            plan.assessment = {**plan.assessment, "alignment": state}
            self.assertEqual(self.blocker(plan), "not_aligned", state)
        plan = plan_with()
        plan.sizing = {"verdict": "UNCHECKED"}
        self.assertEqual(self.blocker(plan), "risk_unchecked")
        plan = plan_with()
        plan.sizing = {"verdict": "REJECTED", "legs": 0}
        self.assertEqual(self.blocker(plan), "risk_rejected")

    def test_it_agrees_with_what_automatic_mode_really_does(self):
        """The funnel must not drift from AutoTrader._judge (plan-level checks only)."""

        class Probe(AutoHarness):
            def runTest(self):
                pass
        probe = Probe()
        probe.setUp()
        probe.turn_on()
        mutations = [
            {}, dict(bias=Bias.NEUTRAL), dict(direction=Direction.SELL), dict(confidence=30.0),
            dict(confidence=59.0), dict(confidence=60.0), dict(advisory_only=True),
            dict(assessment={"verdict": "CAUTION", "verdict_key": "verdict.weak_entry",
                             "alignment": "ALIGNED", "flags": []}),
            dict(assessment={"verdict": "REJECTED", "verdict_key": "verdict.poor_entry",
                             "alignment": "ALIGNED", "flags": []}),
            dict(assessment={"verdict": "APPROVED", "verdict_key": "verdict.approved",
                             "alignment": "CONFLICT", "flags": []}),
            dict(assessment={"verdict": "APPROVED", "verdict_key": "verdict.approved",
                             "alignment": "PARTIAL", "flags": []}),
            dict(sizing={"verdict": "UNCHECKED"}),
            dict(sizing={"verdict": "REJECTED", "legs": 0, "risk_percent": 1.0,
                         "max_money": 1.0, "min_lot_money": 3.0, "currency": "USD"}),
        ]
        for changes in mutations:
            plan = approved_plan()
            for key, value in changes.items():
                setattr(plan, key, value)
            expected = self.blocker(plan).split(":")[0]
            verdict = probe.auto._judge(plan, 0)
            got = funnel.PASS if verdict.accepted else verdict.reason
            self.assertEqual(expected, got, changes)

    def test_the_manual_blocker_is_the_reports_own_reason(self):
        plan = plan_with()
        self.assertEqual(funnel.manual_blocker(plan), "verdict.approved")
        plan = plan_with(advisory_only=True, assessment={})
        self.assertEqual(funnel.manual_blocker(plan), "verdict.no_direction")

    def test_summarise_counts_everything_once(self):
        plans = [plan_with(), plan_with(bias=Bias.NEUTRAL), plan_with(confidence=10.0),
                 plan_with(epic="SILVER")]
        s = funnel.summarise(plans, min_strength=60.0, t=T)
        self.assertEqual(s["n"], 4)
        self.assertEqual(sum(s["auto"].values()), 4)
        self.assertEqual(s["auto"]["pass"], 2)
        self.assertEqual(s["neutral"], 1)
        self.assertEqual(s["directional"], 3)
        self.assertEqual(s["strength_ok"], 2)
        self.assertEqual(set(s["epics"]), {"GOLD", "SILVER"})
        self.assertEqual(s["epics"]["GOLD"]["n"], 3)

    def test_nothing_in_nothing_out(self):
        self.assertEqual(funnel.summarise([], min_strength=60.0, t=T), {"n": 0})


# ---------------------------------------------------------------------- /why

class WhyTests(unittest.TestCase):
    def setUp(self):
        self.sup, self.broker, self.store, self.notifier = build_supervisor()
        self.sup.start()
        self.sup.config.auto.min_signal_strength = 60.0

    def save(self, *plans):
        for i, plan in enumerate(plans):
            plan.plan_id = f"p-{i}"
            plan.created_at = datetime.now(UTC) - timedelta(hours=i)
            self.store.save_plan(plan)

    def test_it_summarises_the_stored_plans_and_names_the_blockers(self):
        weak = plan_with(confidence=30.0)
        weak.assessment = {**weak.assessment, "verdict": "CAUTION", "verdict_key": "verdict.weak_entry",
                           "flags": [{"key": "extended", "severity": "minor", "args": {}}]}
        self.save(plan_with(), plan_with(bias=Bias.NEUTRAL), weak, plan_with(epic="SILVER"))
        text = self.sup.why.command("")
        self.assertIn("4 evaluations, 2 instrument(s)", text)
        self.assertIn("APPROVED 3 (75%)", text)
        self.assertIn("CAUTION 1 (25%)", text)
        self.assertIn("no clear direction", text)
        self.assertIn("verdict is not APPROVED (CAUTION)", text)
        self.assertIn("passed every plan-level check", text)
        self.assertIn("extended 25%", text)
        self.assertIn("reach the 60 that automatic mode needs", text)
        self.assertIn("GOLD: 3 evaluations", text)
        self.assertIn("does not say whether they were right", text)
        self.assertIn("python -m tmbot audit", text)

    def test_one_instrument_and_a_number_of_days(self):
        self.save(plan_with(), plan_with(epic="SILVER"))
        text = self.sup.why.command("SILVER 30")
        self.assertIn("last 30 days: 1 evaluations", text)
        self.assertIn("1 evaluations", text)

    def test_old_plans_are_left_out_of_the_window(self):
        plan = plan_with()
        plan.plan_id, plan.created_at = "old", datetime.now(UTC) - timedelta(days=10)
        self.store.save_plan(plan)
        self.assertIn("No evaluations are stored for the last 7 days", self.sup.why.command(""))
        self.assertIn("1 evaluations", self.sup.why.command("30"))

    def test_it_only_reads_what_is_stored(self):
        self.save(plan_with())
        self.broker.calls.clear()
        self.sup.why.command("")
        self.assertEqual(self.broker.calls, [])

    def test_registered_in_help_and_in_arabic(self):
        seen = {}
        self.sup.notifier.register = lambda name, handler: seen.__setitem__(name, handler)
        self.sup._register_commands()
        self.assertIn("why", seen)
        self.assertIn("/why [symbol] [days]", self.sup.help_text())
        for key, entry in CATALOG.items():
            if key.startswith("why."):
                self.assertTrue(entry.get("ar"), key)
        self.save(plan_with(), plan_with(bias=Bias.NEUTRAL))
        self.sup.t = Translator("ar")
        self.assertNotIn("[why.", self.sup.why.command(""))


# ---------------------------------------------------------------------- the "0.00" report

class NoLotsReportTests(unittest.TestCase):
    def plan(self, equity):
        broker = broker_with()
        broker.set_rules(replace(GOLD, epic="GOLD"))
        broker.account_summary = lambda: {"currency": "USD", "equity": equity}
        return ReportBuilder(broker, auto_config()).build("GOLD")

    def test_with_no_lots_the_loss_shown_is_the_smallest_lots_not_zero(self):
        plan = self.plan(60.0)
        self.assertEqual((plan.sizing["legs"], plan.sizing["total_money"]), (0, 0.0))
        line = next(l for l in render_text(plan).splitlines() if l.startswith("⚠️ Risk"))
        self.assertNotIn("est. loss at SL 0.00", line)
        self.assertIn("no lots fit: the smallest lot (0.01) would lose 3.07 USD at the stop", line)

    def test_with_lots_the_line_is_unchanged(self):
        plan = self.plan(10_000.0)
        line = next(l for l in render_text(plan).splitlines() if l.startswith("⚠️ Risk"))
        self.assertIn("est. loss at SL", line)
        self.assertNotIn("no lots fit", line)

    def test_arabic_has_the_new_line(self):
        text = render_text(self.plan(60.0), Translator("ar"))
        self.assertNotIn("[sm.", text)
        self.assertIn("لا يوجد حجم مناسب", text)


# ---------------------------------------------------------------------- the LLM default

class LlmDefaultTests(unittest.TestCase):
    def test_it_is_off_by_default_and_in_the_example(self):
        import yaml
        from tmbot.config import _coerce
        self.assertFalse(Config().llm.enabled)
        self.assertFalse(_coerce(Config, yaml.safe_load(open("config.example.yaml"))).llm.enabled)

    def test_off_nothing_is_sent_to_claude_even_with_headlines(self):
        from tmbot.analysis import fundamental
        with mock.patch.object(fundamental, "_claude_read", side_effect=AssertionError("called")):
            read = fundamental.analyse("GOLD", "Gold", [{"title": "Gold rallies as inflation "
                                                         "fears rise"}], Config().llm)
        self.assertEqual(read.source, "lexicon")

    def test_it_can_still_be_turned_on(self):
        from tmbot.analysis import fundamental
        config = Config().llm
        config.enabled = True
        with mock.patch.object(fundamental, "_claude_read",
                               side_effect=RuntimeError("called")) as spy:
            fundamental.analyse("GOLD", "Gold", [{"title": "x"}], config)
        self.assertEqual(spy.call_count, 1)


# ---------------------------------------------------------------------- MT5's own calculator

class CalcLossTests(unittest.TestCase):
    def test_mt5_reports_the_loss_the_terminal_itself_calculates(self):
        from tests.test_mt5 import connected
        broker, fake = connected()
        self.assertAlmostEqual(broker.calc_loss("XAUUSDm", Direction.BUY, 0.01, 3410.0, 3407.0), 3.0)
        self.assertAlmostEqual(broker.calc_loss("XAUUSDm", Direction.SELL, 0.05, 3410.0, 3414.0), 20.0)

    def test_an_unknown_symbol_or_a_failing_terminal_gives_none_not_a_crash(self):
        from tests.test_mt5 import connected
        broker, fake = connected()
        self.assertIsNone(broker.calc_loss("NOPE", Direction.BUY, 0.01, 1.0, 0.9))
        fake.order_calc_profit = lambda *a: None
        self.assertIsNone(broker.calc_loss("XAUUSDm", Direction.BUY, 0.01, 3410.0, 3407.0))

    def test_a_broker_with_no_calculator_says_so(self):
        from tmbot.broker.paper import PaperBroker
        self.assertIsNone(PaperBroker().calc_loss("GOLD", Direction.BUY, 0.01, 1.0, 0.9))
        self.assertIsNone(BrokerAdapter.calc_loss(PaperBroker(), "GOLD", Direction.BUY, 1, 1, 1))

    def service(self, theirs_factor):
        sup, broker, store, notifier = build_supervisor()
        sup.start()
        broker.set_rules(replace(RULES, epic="GOLD", value_per_point=100.0,
                                 min_deal_size=0.01, size_step=0.01))
        broker.account_summary = lambda: {"currency": "USD", "equity": 10_000.0}
        broker.calc_loss = lambda epic, d, vol, entry, stop: (
            abs(entry - stop) * 100.0 * vol * theirs_factor)
        return sup

    def test_the_afford_command_puts_the_two_numbers_side_by_side(self):
        text = self.service(1.0).afford.command("")
        self.assertIn("Cross-check with the broker's own profit calculator", text)
        self.assertIn("✅ GOLD", text)
        self.assertIn("(0.00% apart)", text)

    def test_a_real_disagreement_is_flagged_loudly(self):
        text = self.service(2.0).afford.command("")
        self.assertIn("⚠️ GOLD", text)
        self.assertIn("may be wrong", text)
        self.assertIn("do not trust its lot sizes", text)

    def test_a_small_difference_inside_the_tolerance_is_fine(self):
        self.assertIn("✅ GOLD", self.service(1.005).afford.command(""))

    def test_the_check_is_informational_it_changes_no_decision(self):
        good = self.service(1.0)
        bad = self.service(2.0)
        self.assertEqual(good.afford.compute("GOLD").affordable, bad.afford.compute("GOLD").affordable)

    def test_without_a_calculator_there_is_no_check_section(self):
        sup, broker, store, notifier = build_supervisor()
        sup.start()
        broker.set_rules(replace(RULES, epic="GOLD", value_per_point=100.0))
        broker.account_summary = lambda: {"currency": "USD", "equity": 10_000.0}
        self.assertNotIn("Cross-check", sup.afford.command(""))


# ---------------------------------------------------------------------- sizing, per instrument

# ILLUSTRATIVE specifications, one per supported instrument, for exercising the
# maths. They are NOT your broker's values: read yours from MetaTrader 5
# (Market Watch > right-click > Specification) or run /afford.
SPECS = {
    "XAUUSDm": dict(value=100.0, lot=0.01, step=0.01, maximum=100.0, price=3400.0, stop=3.0),
    "UKOILm": dict(value=1000.0, lot=0.01, step=0.01, maximum=100.0, price=80.0, stop=0.8),
    "USOILm": dict(value=1000.0, lot=0.01, step=0.01, maximum=100.0, price=76.0, stop=0.8),
    "XNGUSDm": dict(value=10000.0, lot=0.01, step=0.01, maximum=100.0, price=3.0, stop=0.05),
    "EURUSDm": dict(value=100000.0, lot=0.01, step=0.01, maximum=100.0, price=1.08, stop=0.0010),
    "XAGUSDm": dict(value=5000.0, lot=0.01, step=0.01, maximum=100.0, price=45.0, stop=0.35),
}


def rules_for(epic, spec):
    return replace(RULES, epic=epic, value_per_point=spec["value"], min_deal_size=spec["lot"],
                   size_step=spec["step"], max_deal_size=spec["maximum"], decimal_places=5)


class SizingInvariantTests(unittest.TestCase):
    def assess(self, epic, equity, risk=1.0, stop=None, spread=0.0, commission=0.0, slippage=0.0):
        spec = SPECS[epic]
        return risk_module.assess(
            equity=equity, currency="USD", risk_percent=risk, stop_distance=stop or spec["stop"],
            rules=rules_for(epic, spec), legs_planned=3, spread=spread,
            commission_per_lot=commission, slippage=slippage)

    def test_the_loss_never_exceeds_the_limit_for_any_instrument_or_account(self):
        for epic in SPECS:
            for equity in (50, 75, 100, 250, 1000, 10_000, 100_000):
                for risk in (1.0, 2.0, 3.0):
                    a = self.assess(epic, equity, risk, spread=SPECS[epic]["stop"] * 0.05)
                    self.assertLessEqual(a.total_money, a.max_money + 1e-9, (epic, equity, risk))

    def test_a_volume_below_the_broker_minimum_is_never_returned(self):
        for epic, spec in SPECS.items():
            for equity in (50, 100, 1000, 100_000):
                a = self.assess(epic, equity)
                self.assertTrue(a.legs == 0 or a.per_leg >= spec["lot"] - 1e-12, (epic, equity))

    def test_every_volume_is_a_multiple_of_the_step_and_below_the_maximum(self):
        for epic, spec in SPECS.items():
            for equity in (500, 5000, 5_000_000):
                a = self.assess(epic, equity)
                if a.legs:
                    steps = a.per_leg / spec["step"]
                    self.assertAlmostEqual(steps, round(steps), places=6, msg=(epic, equity))
                    self.assertLessEqual(a.per_leg, spec["maximum"] + 1e-9)

    def test_zero_volume_always_means_the_smallest_lot_really_is_too_big(self):
        """The 0.00 case: no lots <=> the minimum lot's loss exceeds the limit."""
        zero = 0
        for epic, spec in SPECS.items():
            for equity in (50, 75, 100, 149, 250, 1000):
                for risk in (1.0, 2.0, 3.0):
                    a = self.assess(epic, equity, risk)
                    smallest = (spec["stop"] + 0) * spec["value"] * spec["lot"]
                    if a.legs == 0:
                        zero += 1
                        self.assertEqual((a.verdict, a.selected_volume, a.total_money),
                                         (risk_module.REJECTED, 0.0, 0.0))
                        self.assertGreater(smallest, a.max_money, (epic, equity, risk))
                        self.assertAlmostEqual(a.min_lot_money, smallest, places=2)
                    else:
                        self.assertLessEqual(smallest, a.max_money + 1e-9, (epic, equity, risk))
        self.assertGreater(zero, 10, "the table must actually exercise the zero-volume case")

    def test_the_figures_in_the_reject_message_are_the_real_smallest_lot_loss(self):
        a = self.assess("XAGUSDm", 149.33, risk=3.0, stop=0.353)
        self.assertEqual(a.legs, 0)
        self.assertAlmostEqual(a.min_lot_money, 0.353 * 5000.0 * 0.01, places=2)
        self.assertAlmostEqual(a.max_money, 4.48, places=2)
        self.assertGreater(a.min_lot_money, a.max_money)

    def test_random_inputs_never_break_an_invariant(self):
        rnd = random.Random(7)
        for _ in range(3000):
            epic = rnd.choice(list(SPECS))
            spec = SPECS[epic]
            a = self.assess(epic, rnd.uniform(40, 200_000), rnd.choice((1.0, 2.0, 3.0)),
                            stop=spec["stop"] * rnd.uniform(0.2, 6.0),
                            spread=spec["stop"] * rnd.uniform(0, 0.4),
                            commission=rnd.choice((0.0, 3.0, 7.0)),
                            slippage=spec["stop"] * rnd.uniform(0, 0.2))
            self.assertLessEqual(a.total_money, a.max_money + 1e-9)
            self.assertGreaterEqual(a.legs, 0)
            self.assertLessEqual(a.legs, 3)
            self.assertEqual(a.verdict == risk_module.REJECTED, a.legs == 0)
            if a.legs:
                self.assertGreaterEqual(a.per_leg, spec["lot"] - 1e-12)

    def test_costs_only_ever_shrink_the_position(self):
        for epic in SPECS:
            plain = self.assess(epic, 20_000)
            costly = self.assess(epic, 20_000, spread=SPECS[epic]["stop"] * 0.2, commission=7.0,
                                 slippage=SPECS[epic]["stop"] * 0.1)
            self.assertLessEqual(costly.selected_volume, plain.selected_volume)


class RiskSettingSourceTests(unittest.TestCase):
    def setUp(self):
        self.sup, self.broker, self.store, self.notifier = build_supervisor()
        self.broker.set_rules(replace(RULES, epic="GOLD", value_per_point=100.0,
                                      min_deal_size=0.01, size_step=0.01))
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 1000.0}

    def test_config_is_used_until_a_choice_is_saved(self):
        self.assertEqual(self.sup.config.report.risk_percent, 1.0)
        self.assertIn("from config.yaml", self.sup.auto.risk_command(""))

    def test_a_saved_choice_wins_over_config_and_says_so(self):
        self.sup.auto.risk_command("3")
        text = self.sup.auto.risk_command("")
        self.assertIn("chosen with /risk and saved in the database", text)
        self.assertIn("config.yaml says 1%", text)
        # a restart: config.yaml is read again, then the saved choice replaces it
        self.sup.config.report.risk_percent = 1.0
        self.sup.auto.load_risk_override()
        self.assertEqual(self.sup.config.report.risk_percent, 3.0)

    def test_every_consumer_reads_the_same_effective_value(self):
        self.sup.auto.risk_command("2")
        plan = ReportBuilder(self.broker, self.sup.config).build("GOLD")
        self.assertEqual(plan.sizing["risk_percent"], 2.0)
        self.assertEqual(self.sup.auto.risk_percent(), 2.0)

    def test_choosing_the_config_value_again_stops_calling_it_saved_over_config(self):
        self.sup.auto.risk_command("2")
        self.sup.auto.risk_command("1")
        self.assertIn("from config.yaml", self.sup.auto.risk_command(""))


# ---------------------------------------------------------------------- replay

def series(n=2400, drift=0.0, wave=0.0, seed=3, start=3400.0):
    rnd = random.Random(seed)
    out, price = [], start
    for i in range(n):
        o = price
        price = price * (1 + drift + rnd.gauss(0, 0.0004) + wave * __import__("math").sin(i / 40))
        out.append(Candle(ts=BASE + timedelta(minutes=5 * i), open=o,
                          high=max(o, price) * 1.0003, low=min(o, price) * 0.9997, close=price))
    return out


def aggregate(bars, k):
    return [Candle(ts=bars[i].ts, open=bars[i].open, high=max(b.high for b in bars[i:i + k]),
                   low=min(b.low for b in bars[i:i + k]), close=bars[i + k - 1].close)
            for i in range(0, len(bars) - k + 1, k)]


def histories(**kw):
    m5 = series(**kw)
    return {"M5": m5, "M15": aggregate(m5, 3), "H1": aggregate(m5, 12),
            "H4": aggregate(m5, 48), "D1": aggregate(m5, 288)}


class OutcomeTests(unittest.TestCase):
    def bar(self, low, high, close=None):
        return Candle(ts=BASE, open=low, high=high, low=low, close=close if close is not None else high)

    def test_a_target_reached_before_the_stop(self):
        out = replay.outcome(Direction.BUY, 100.0, 98.0, [102.0, 104.0, 106.0],
                             [self.bar(99, 102.5), self.bar(100, 104.5), self.bar(101, 105)], 10)
        self.assertEqual(out["state"], ["hit", "hit", "open"])
        self.assertEqual(out["r"][:2], [1.0, 2.0])
        self.assertTrue(out["tp1_first"])
        self.assertFalse(out["stop_first"])

    def test_the_stop_first(self):
        out = replay.outcome(Direction.BUY, 100.0, 98.0, [102.0, 104.0, 106.0],
                             [self.bar(97.5, 100.5), self.bar(100, 103)], 10)
        self.assertEqual(out["state"], ["stop", "stop", "stop"])
        self.assertEqual(out["r"], [-1.0, -1.0, -1.0])
        self.assertTrue(out["stop_first"])

    def test_a_bar_touching_both_counts_as_the_stop(self):
        out = replay.outcome(Direction.BUY, 100.0, 98.0, [102.0, 104.0, 106.0],
                             [self.bar(97.0, 103.0)], 10)
        self.assertEqual(out["state"][0], "stop")

    def test_still_open_is_marked_to_the_last_close(self):
        out = replay.outcome(Direction.BUY, 100.0, 98.0, [102.0, 104.0, 106.0],
                             [self.bar(99, 101, close=101.0)], 10)
        self.assertEqual(out["state"], ["open", "open", "open"])
        self.assertEqual(out["r"], [0.5, 0.5, 0.5])

    def test_a_short_mirrors_it(self):
        out = replay.outcome(Direction.SELL, 100.0, 102.0, [98.0, 96.0, 94.0],
                             [self.bar(97.5, 100.5), self.bar(95.5, 99)], 10)
        self.assertEqual(out["state"], ["hit", "hit", "open"])

    def test_the_horizon_limits_how_far_it_looks(self):
        later = [self.bar(99, 101)] * 3 + [self.bar(99, 103)]
        self.assertEqual(replay.outcome(Direction.BUY, 100.0, 98.0, [102.0], later, 3)["state"], ["open"])
        self.assertEqual(replay.outcome(Direction.BUY, 100.0, 98.0, [102.0], later, 4)["state"], ["hit"])

    def test_nothing_after_or_no_risk_gives_nothing_not_a_guess(self):
        self.assertEqual(replay.outcome(Direction.BUY, 100.0, 98.0, [102.0], [], 10)["r"], [])
        self.assertEqual(replay.outcome(Direction.BUY, 100.0, 100.0, [102.0], [self.bar(99, 103)], 10)["r"], [])


class ReplayBrokerTests(unittest.TestCase):
    def setUp(self):
        from tmbot.broker.paper import PaperBroker
        self.real = PaperBroker()
        self.real.set_rules(replace(GOLD, epic="GOLD"))
        self.real.set_quote("GOLD", 3400.0, 3400.4)
        self.real.account_summary = lambda: {"currency": "USD", "equity": 55.0}
        self.h = histories()
        self.broker = replay.ReplayBroker(self.real, self.h)

    def test_it_can_only_see_the_past(self):
        cutoff = self.h["M15"][500].ts
        self.broker.set_time(cutoff)
        for tf in ("M5", "M15", "H1", "H4", "D1"):
            bars = self.broker.candles("GOLD", tf, 10_000)
            self.assertTrue(bars)
            self.assertLessEqual(bars[-1].ts, cutoff, tf)

    def test_the_quote_is_the_last_known_price_with_the_real_spread(self):
        self.broker.set_time(self.h["M5"][100].ts)
        quote = self.broker.quote("GOLD")
        self.assertAlmostEqual(quote.mid, self.h["M5"][100].close, places=6)
        self.assertAlmostEqual(quote.spread, 0.4, places=6)

    def test_equity_can_be_overridden_to_isolate_quality_from_affordability(self):
        self.assertEqual(self.broker.account_summary()["equity"], 55.0)
        big = replay.ReplayBroker(self.real, self.h, equity=1_000_000.0)
        self.assertEqual(big.account_summary()["equity"], 1_000_000.0)

    def test_it_has_no_way_to_trade(self):
        for name in ("open_position", "close_position", "modify_position", "positions"):
            self.assertFalse(hasattr(self.broker, name), name)
        self.assertNotIsInstance(self.broker, BrokerAdapter)


class ReplayTests(unittest.TestCase):
    def run_replay(self, **kw):
        from tmbot.broker.paper import PaperBroker
        real = PaperBroker()
        real.set_rules(replace(GOLD, epic="GOLD"))
        real.set_quote("GOLD", 3400.0, 3400.2)
        real.account_summary = lambda: {"currency": "USD", "equity": 1_000_000.0}
        broker = replay.ReplayBroker(real, histories(**kw), equity=1_000_000.0)
        spy = []
        original = broker.candles

        def checked(epic, timeframe, limit):
            bars = original(epic, timeframe, limit)
            spy.append((broker.cutoff, bars[-1].ts if bars else None))
            return bars
        broker.candles = checked
        records = replay.replay(ReportBuilder(broker, auto_config()), broker, "GOLD", step=24,
                                horizon=48, min_strength=60.0, t=T, warmup=300)
        return records, spy

    def test_no_decision_ever_sees_a_candle_from_its_own_future(self):
        records, spy = self.run_replay(drift=0.00006)
        self.assertGreater(len(records), 10)
        self.assertTrue(spy)
        self.assertTrue(all(last is None or last <= cutoff for cutoff, last in spy))

    def test_a_steady_rise_makes_long_setups_that_reach_their_first_target(self):
        records, _ = self.run_replay(drift=0.00008)
        directional = [r for r in records if r.directional and r.outcome.get("r")]
        self.assertGreater(len(directional), 10)
        share = sum(r.outcome["tp1_first"] for r in directional) / len(directional)
        self.assertGreater(share, 0.7, share)

    def test_the_summary_groups_every_way_and_reads_back_sensibly(self):
        records, _ = self.run_replay(drift=0.00006)
        s = replay.summarise(records)
        self.assertEqual(s["records"], len(records))
        self.assertEqual(s["all"]["n"], len([r for r in records if r.outcome.get("r")]))
        self.assertEqual(sum(g["n"] for g in s["by_verdict"].values()), s["directional"]["n"])
        self.assertEqual(sum(g["n"] for g in s["by_strength"].values()), s["directional"]["n"])
        for pair in s["by_flag"].values():
            self.assertEqual(pair["with"]["n"] + pair["without"]["n"], s["directional"]["n"])
        text = replay.render(s, epic="GOLD", step=24, horizon=48, equity=None)
        for heading in ("AUDIT GOLD", "By the manual verdict", "By signal strength",
                        "By higher-timeframe alignment", "Each quality warning",
                        "By what stops automatic mode", "How to read it", "Past results, not a forecast"):
            self.assertIn(heading, text)

    def test_small_groups_are_shown_without_a_verdict(self):
        few = [replay.Record(ts=BASE, verdict="APPROVED", quality="GOOD", alignment="ALIGNED",
                             bias="BULLISH", strength=70.0, flags=[], blocker="pass", sizing="OK",
                             directional=True,
                             outcome={"r": [1.0, 2.0, 3.0], "tp1_first": True, "stop_first": False,
                                      "state": ["hit"] * 3}) for _ in range(5)]
        g = replay.group_stats(few)
        self.assertEqual((g["n"], g["enough"], g["interval"]), (5, False, None))
        self.assertIn("too few to judge", replay.render(replay.summarise(few), epic="G", step=1,
                                                        horizon=1, equity=None))

    def test_not_enough_history_returns_nothing_instead_of_inventing_a_result(self):
        from tmbot.broker.paper import PaperBroker
        broker = replay.ReplayBroker(PaperBroker(), {"M15": series(100)})
        self.assertEqual(replay.replay(None, broker, "GOLD", step=1, horizon=10, min_strength=60.0,
                                       t=T), [])

    def test_the_command_line_has_an_audit_command(self):
        import io, contextlib
        from tmbot import cli
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["audit", "--help"])


# ---------------------------------------------------------------------- scenarios from the brief

class ScenarioTests(unittest.TestCase):
    def test_spread_that_is_too_large_for_the_stop_is_flagged_and_breaks_the_entry(self):
        flags = quality_module.assess(
            bias=Bias.BULLISH, direction=Direction.BUY, price=100.0, atr=1.0, ema_fast=99.9,
            rsi=55.0, stop=99.6, tp1=100.5, levels=[], spread=0.4, tp1_pushed=False,
            higher_timeframe="H4", higher_bias=Bias.BULLISH, risk_verdict="OK").flags
        self.assertIn("wide_spread", [f.key for f in flags])
        zone = entry_range.build(
            direction_sign=1, reference=100.0, stop=99.6, targets=[100.5, 100.9, 101.4], atr=1.0,
            spread=0.01, slippage=0.0, commission_per_lot=0.0, value_per_point=100.0,
            sizing={"per_leg": 0.1, "legs": 3, "max_money": 200.0, "risk_percent": 1.0,
                    "currency": "USD"}, deal_targets=(1, 2, 3), min_reward_risk=0.8,
            min_stop_atr=0.3, created_at=datetime.now(UTC), valid_hours=4.0, bias="BULLISH")
        valid, reasons = entry_range.check(zone, price=100.0, spread=0.4, now=datetime.now(UTC))
        self.assertFalse(valid, "a spread this wide also pushes the entry out of range")
        self.assertIn("reward_risk_low", {k for k, _ in reasons})

    def test_a_setup_whose_conditions_have_changed_is_invalidated(self):
        zone = plan_with().entry_zone
        now = datetime(2026, 10, 27, 10, 5, tzinfo=UTC)
        self.assertTrue(entry_range.check(zone, price=3400.0, spread=0.0, now=now,
                                          latest_bias="BULLISH")[0])
        valid, reasons = entry_range.check(zone, price=3400.0, spread=0.0, now=now,
                                           latest_bias="BEARISH")
        self.assertFalse(valid)
        self.assertEqual([k for k, _ in reasons], ["bias_changed"])

    def test_price_that_left_the_entry_range_is_out(self):
        zone = plan_with().entry_zone
        now = datetime(2026, 10, 27, 10, 5, tzinfo=UTC)
        keys = [k for k, _ in entry_range.check(zone, price=3400.6, spread=0.0, now=now)[1]]
        self.assertIn("outside_range", keys)
        # one price excursion trips three overlapping rules (a finding: they restate each other)
        self.assertEqual(set(keys), {"outside_range", "risk_over_limit", "reward_risk_low"})


# ---------------------------------------------------------------------- findings, pinned

class FindingsPinnedTests(unittest.TestCase):
    """These assert what the code does TODAY. Each backs a finding in the audit. If a
    finding is later fixed, the matching test is updated on purpose -- that is the point."""

    def test_F1_without_news_the_signal_strength_cannot_exceed_70(self):
        from tmbot.analysis import bias as bias_module, fundamental, report
        technical = mock.Mock(confidence=100.0, score=100.0, bias=Bias.BULLISH)
        absent = fundamental.FundamentalRead(bias=Bias.NEUTRAL, confidence=0.0, summary="",
                                             source="skipped")
        self.assertEqual(report._combined_confidence(technical, absent), 70.0)

    def test_F1_so_automatic_mode_at_60_needs_a_technical_confidence_above_85(self):
        from tmbot.analysis import fundamental, report
        absent = fundamental.FundamentalRead(bias=Bias.NEUTRAL, confidence=0.0, summary="",
                                             source="skipped")
        for technical_confidence, passes in ((85.0, False), (85.8, True), (70.0, False)):
            technical = mock.Mock(confidence=technical_confidence, bias=Bias.BULLISH)
            got = report._combined_confidence(technical, absent) >= 60.0
            self.assertEqual(got, passes, technical_confidence)

    def test_F2_the_missing_news_also_shrinks_the_technical_score_before_the_20_threshold(self):
        from tmbot.analysis import fundamental, report
        absent = fundamental.FundamentalRead(bias=Bias.NEUTRAL, confidence=0.0, summary="",
                                             source="skipped")
        for score, expected in ((28.0, Bias.NEUTRAL), (28.6, Bias.BULLISH), (-28.0, Bias.NEUTRAL)):
            technical = mock.Mock(score=score)
            self.assertEqual(report._combine(technical, absent), expected, score)
        self.assertEqual(absent.source, "skipped")      # the module says it is "excluded"

    def test_F3_a_strong_trend_alone_can_make_three_minor_flags_and_a_rejection(self):
        """extended + RSI stretched + TP1 pushed = POOR -> REJECTED, with no major flag."""
        result = quality_module.assess(
            bias=Bias.BULLISH, direction=Direction.BUY, price=110.0, atr=2.0, ema_fast=105.0,
            rsi=78.0, stop=106.0, tp1=112.0, levels=[], spread=0.0, tp1_pushed=True,
            higher_timeframe="H4", higher_bias=Bias.BULLISH, risk_verdict="OK")
        self.assertEqual({f.key for f in result.flags}, {"extended", "rsi_stretched", "tp1_pushed"})
        self.assertTrue(all(f.severity == "minor" for f in result.flags))
        self.assertEqual((result.quality, result.verdict), ("POOR", "REJECTED"))

    def test_F4_one_minor_flag_is_enough_to_stop_automatic_mode(self):
        result = quality_module.assess(
            bias=Bias.BULLISH, direction=Direction.BUY, price=110.0, atr=2.0, ema_fast=108.0,
            rsi=78.0, stop=106.0, tp1=116.0, levels=[], spread=0.0, tp1_pushed=False,
            higher_timeframe="H4", higher_bias=Bias.BULLISH, risk_verdict="OK")
        self.assertEqual([f.key for f in result.flags], ["rsi_stretched"])
        self.assertEqual((result.quality, result.verdict), ("WEAK", "CAUTION"))

    def test_F5_the_same_rsi_reading_both_adds_to_the_bias_and_counts_against_the_entry(self):
        from tmbot.analysis import bias as bias_module
        factors = {}
        closes = [100 + i * 0.5 for i in range(260)]
        candles = [Candle(ts=BASE + timedelta(hours=i), open=c, high=c + 0.3, low=c - 0.3, close=c)
                   for i, c in enumerate(closes)]
        read = bias_module.analyse(candles, Config().analysis)
        rsi_factor = next(f for f in read.factors if f.name == "rsi")
        self.assertGreater(rsi_factor.value, 0.9)           # RSI ~100 votes strongly bullish ...
        self.assertGreaterEqual(read.rsi, quality_module.RSI_STRETCHED[1])   # ... and is "stretched"

    def test_F6_a_plan_with_tp1_at_the_minimum_reward_risk_already_scores_56_before_any_warning(self):
        from tmbot.analysis import report
        plan = approved_plan()
        plan.tp1 = plan.reference_price + 0.8 * plan.risk
        plan.assessment = {**plan.assessment, "flags": []}
        plan.technical = {**plan.technical, "adx": 40}
        self.assertAlmostEqual(report.loss_probability(plan), 1 / 1.8, places=3)
        self.assertLess(report.loss_probability(plan), report.BIG_LOSS_CHANCE)
        plan.assessment = {**plan.assessment, "flags": [
            {"key": "extended", "severity": "minor", "args": {}},
            {"key": "rsi_stretched", "severity": "minor", "args": {}}]}
        self.assertGreaterEqual(report.loss_probability(plan), report.BIG_LOSS_CHANCE)

    def test_F7_fundamentals_off_in_now_changes_the_answer_you_get_from_the_same_data(self):
        """/now skips news (fundamentals=False); /report with no news source is the same
        neutral read -- but with a news source /report differs from /now for one market."""
        broker = broker_with()
        plan_now = ReportBuilder(broker, auto_config()).build("GOLD", fundamentals=False)
        self.assertEqual(plan_now.fundamental["source"], "skipped")
        self.assertEqual(plan_now.fundamental["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
