"""What the account can afford: the maths, /afford, the report lines, the auto-mode skip."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest import mock

from tmbot.analysis import afford, risk as risk_module
from tmbot.analysis.report import ReportBuilder, render_full, render_text
from tmbot.errors import RetryableError
from tmbot.i18n import CATALOG, Translator
from tmbot.manage import auto as auto_module
from tests.helpers import RULES, candles
from tests.test_style import GOLD, auto_config, broker_with
from tests.test_supervisor import build_supervisor, texts

UTC = timezone.utc


def assess(**kw):
    base = dict(epic="XAG", equity=149.33, risk_percent=3.0, value_per_point=5000.0,
                min_lot=0.01, atr=0.5, min_stop_atr=0.6)
    base.update(kw)
    return afford.assess(**base)


class MathTests(unittest.TestCase):
    def test_a_small_account_cannot_take_silvers_smallest_lot(self):
        a = assess()                      # 0.01 lot of 5000 oz = 50 per $1; limit 3% of 149.33
        self.assertAlmostEqual(a.max_money, 4.48, places=2)
        self.assertAlmostEqual(a.max_stop, 4.4799 / 50.0, places=3)         # 0.0896
        self.assertAlmostEqual(a.tight_stop, 0.3)
        self.assertAlmostEqual(a.tight_loss, 0.3 * 50.0)                    # 15.00
        self.assertAlmostEqual(a.min_equity_tight, 15.0 / 0.03, places=1)   # 500
        self.assertTrue(a.known)
        self.assertFalse(a.affordable)
        self.assertTrue(a.unaffordable)

    def test_the_line_between_affordable_and_not_is_the_tightest_stop(self):
        for equity, expected in ((100.0, False), (239.0, False), (240.0, True), (300.0, True)):
            a = afford.assess(epic="XAU", equity=equity, risk_percent=1.0, value_per_point=100.0,
                              min_lot=0.01, atr=4.0, min_stop_atr=0.6)
            self.assertEqual(a.affordable, expected, equity)
        exact = afford.assess(epic="XAU", equity=240.0, risk_percent=1.0, value_per_point=100.0,
                              min_lot=0.01, atr=4.0, min_stop_atr=0.6)
        self.assertAlmostEqual(exact.max_stop, exact.tight_stop, places=9)  # exactly on the line

    def test_the_loss_at_the_widest_stop_is_exactly_the_limit(self):
        a = assess(equity=1000.0, risk_percent=1.0, spread=0.1, slippage=0.05,
                   commission_per_lot=7.0)
        loss = afford.loss_at(a.max_stop, value=a.value, lot=a.min_lot, cost=a.cost,
                              commission=a.commission)
        self.assertAlmostEqual(loss, a.max_money, places=6)

    def test_more_equity_always_means_a_wider_affordable_stop(self):
        stops = [assess(equity=e).max_stop for e in (50, 100, 150, 300, 600)]
        self.assertEqual(stops, sorted(stops))

    def test_a_higher_risk_percent_widens_it_but_only_by_the_percent(self):
        low, high = assess(risk_percent=1.0), assess(risk_percent=3.0)
        self.assertAlmostEqual(high.max_stop / low.max_stop, 3.0, places=5)

    def test_costs_make_it_harder_never_easier(self):
        plain = assess(equity=1000.0)
        for extra in (dict(spread=0.2), dict(slippage=0.1), dict(commission_per_lot=7.0)):
            self.assertLess(assess(equity=1000.0, **extra).max_stop, plain.max_stop, extra)
            self.assertGreater(assess(equity=1000.0, **extra).tight_loss, plain.tight_loss, extra)

    def test_a_commission_bigger_than_the_whole_limit_is_never_affordable(self):
        a = assess(equity=100.0, risk_percent=1.0, commission_per_lot=500.0)
        self.assertLess(a.max_stop, 0)
        self.assertFalse(a.affordable)

    def test_the_equity_needed_follows_the_risk_percent(self):
        one = assess(risk_percent=1.0)
        three = assess(risk_percent=3.0)
        self.assertAlmostEqual(one.min_equity_tight, 3 * three.min_equity_tight, places=3)

    def test_the_last_plans_stop_gets_its_own_figure(self):
        a = assess(typical_stop=0.353)
        self.assertAlmostEqual(a.typical_loss, 0.353 * 50.0)
        self.assertAlmostEqual(a.min_equity_typical, 0.353 * 50.0 / 0.03, places=2)
        self.assertIsNone(assess().typical_stop)

    def test_unknown_inputs_mean_unknown_never_a_guess(self):
        for kw, why in ((dict(value_per_point=None), "no_value"), (dict(value_per_point=0), "no_value"),
                        (dict(min_lot=None), "no_lot"), (dict(min_lot=0), "no_lot"),
                        (dict(atr=None), "no_atr"), (dict(atr=0), "no_atr"),
                        (dict(equity=None), "no_equity"), (dict(equity=0), "no_equity"),
                        (dict(risk_percent=0), "no_equity")):
            a = assess(**kw)
            self.assertFalse(a.known, kw)
            self.assertEqual(a.why_unknown, why, kw)
            self.assertFalse(a.affordable, kw)
            self.assertFalse(a.unaffordable, "unknown must never count as unaffordable")

    def test_the_minimum_equity_helper(self):
        self.assertAlmostEqual(afford.minimum_equity(3.1, 1.0), 310.0)
        self.assertIsNone(afford.minimum_equity(0, 1.0))
        self.assertIsNone(afford.minimum_equity(3.1, 0))

    def test_it_agrees_with_the_real_sizing_rule(self):
        """/afford says "no" exactly when the risk module would reject at the tightest stop."""
        rules = replace(GOLD, epic="GOLD")
        for equity in (50, 100, 150, 250, 400, 1000):
            a = afford.assess(epic="GOLD", equity=equity, risk_percent=1.0, value_per_point=100.0,
                              min_lot=0.01, atr=4.0, min_stop_atr=0.6)
            real = risk_module.assess(equity=equity, currency="USD", risk_percent=1.0,
                                      stop_distance=a.tight_stop, rules=rules, legs_planned=3)
            self.assertEqual(a.affordable, real.verdict != risk_module.REJECTED, equity)


class Harness(unittest.TestCase):
    def setUp(self):
        self.sup, self.broker, self.store, self.notifier = build_supervisor()
        self.sup.start()
        self.broker.set_rules(replace(RULES, epic="GOLD", value_per_point=100.0,
                                      min_deal_size=0.01, size_step=0.01))
        self.equity = 100.0
        self.broker.account_summary = lambda: {"currency": "USD", "equity": self.equity}
        self.service = self.sup.afford

    def add_silver(self, value=5000.0):
        self.broker.set_rules(replace(RULES, epic="SILVER", value_per_point=value,
                                      min_deal_size=0.01, size_step=0.01))
        self.broker.set_candles("SILVER", "H1", candles(300, start=45.0, drift=0.02, wave=0.3,
                                                         spread=0.1))
        self.broker.set_quote("SILVER", 45.0, 45.02)
        self.sup.config.analysis.watchlist.append(
            replace(self.sup.config.analysis.watchlist[0], epic="SILVER", display="Silver"))


class ServiceTests(Harness):
    def test_the_answer_is_cached_and_force_refreshes_it(self):
        calls = []
        real = self.broker.account_summary

        def counting():
            calls.append(1)
            return real()
        self.broker.account_summary = counting
        self.service.compute("GOLD")
        self.service.compute("GOLD")
        self.assertEqual(len(calls), 1)
        self.service.compute("GOLD", force=True)
        self.assertEqual(len(calls), 2)

    def test_gold_is_unaffordable_at_a_hundred_dollars_and_fine_with_more(self):
        self.assertIsNotNone(self.service.unaffordable("GOLD"))
        self.equity = 100_000.0
        self.service.compute("GOLD", force=True)
        self.assertIsNone(self.service.unaffordable("GOLD"))

    def test_nothing_is_skipped_on_a_guess(self):
        self.broker.set_rules(replace(RULES, epic="GOLD", value_per_point=None))
        self.assertIsNone(self.service.unaffordable("GOLD"))
        self.assertFalse(self.service.compute("GOLD", force=True).known)

    def test_a_broker_error_is_unknown_not_unaffordable(self):
        def down():
            raise RetryableError("offline")
        self.broker.account_summary = down
        self.assertIsNone(self.service.unaffordable("GOLD"))
        self.assertEqual(self.service.compute("GOLD", force=True).why_unknown, "no_equity")

    def test_the_figures_use_the_configured_costs_and_risk(self):
        base = self.service.compute("GOLD", force=True)
        self.sup.config.report.commission_per_lot = 7.0
        self.sup.config.report.risk_percent = 3.0
        again = self.service.compute("GOLD", force=True)
        self.assertEqual(again.risk_percent, 3.0)
        self.assertGreater(again.commission, base.commission)

    def test_asking_changes_no_rule(self):
        before = (self.sup.config.report.risk_percent, self.sup.config.analysis.min_stop_atr,
                  self.sup.config.report.commission_per_lot)
        self.service.command("")
        self.assertEqual(before, (self.sup.config.report.risk_percent,
                                  self.sup.config.analysis.min_stop_atr,
                                  self.sup.config.report.commission_per_lot))


class CommandTests(Harness):
    def test_the_heading_shows_equity_risk_and_the_money_limit(self):
        text = self.service.command("")
        self.assertIn("equity 100.00 USD, risk 1% = at most 1.00 lost per trade", text)

    def test_each_instrument_gets_a_clear_line_with_the_equity_it_needs(self):
        text = self.service.command("")
        gold = next(l for l in text.splitlines() if "GOLD" in l)
        self.assertTrue(gold.startswith("❌"))
        self.assertIn("even the tightest stop the bot uses", gold)
        self.assertIn("over your 1.00 limit", gold)
        self.assertIn("needs equity of about", gold)
        self.assertIn("at 1%", gold)

    def test_an_affordable_instrument_says_how_wide_a_stop_it_can_take(self):
        self.equity = 100_000.0
        gold = next(l for l in self.service.command("").splitlines() if "GOLD" in l)
        self.assertTrue(gold.startswith("✅"))
        self.assertIn("fits a stop up to", gold)
        self.assertIn("ATR", gold)

    def test_unknown_is_shown_as_unknown_and_not_skipped(self):
        self.broker.set_rules(replace(RULES, epic="GOLD", value_per_point=None))
        gold = next(l for l in self.service.command("").splitlines() if "GOLD" in l)
        self.assertTrue(gold.startswith("❔"))
        self.assertIn("the broker publishes no contract value", gold)
        self.assertIn("it is not skipped", gold)

    def test_affordable_instruments_are_listed_first(self):
        self.add_silver(value=1.0)               # tiny value per point: fits easily
        lines = [l for l in self.service.command("").splitlines() if l[:1] in "✅❌❔"]
        self.assertEqual([l[0] for l in lines], ["✅", "❌"])

    def test_one_symbol_can_be_asked_for(self):
        self.add_silver()
        text = self.service.command("SILVER")
        self.assertIn("SILVER", text)
        self.assertNotIn("GOLD", text)

    def test_the_last_plans_stop_is_shown_when_there_is_one(self):
        self.sup._plan_for("GOLD", force=True)
        gold = next(l for l in self.service.command("").splitlines() if "GOLD" in l)
        self.assertIn("your last plan's stop", gold)

    def test_it_says_it_changes_no_rule(self):
        self.assertIn("This changes no rule", self.service.command(""))

    def test_without_equity_it_says_so(self):
        def down():
            raise RetryableError("offline")
        self.broker.account_summary = down
        self.assertIn("equity could not be read", self.service.command(""))

    def test_an_empty_watchlist_is_handled(self):
        self.sup.config.analysis.watchlist = []
        self.assertEqual(self.service.command(""), "No instruments to check.")

    def test_arabic_has_every_string_and_renders_whole(self):
        for key, entry in CATALOG.items():
            if key.startswith("afford.") or key in ("tr.min_equity", "tr.action_skip_needs",
                                                     "auto.none_affordable",
                                                     "auto.reject.unaffordable"):
                self.assertTrue(entry.get("ar"), key)
        self.add_silver()
        self.sup.t = Translator("ar")
        for argument in ("", "SILVER"):
            self.assertNotIn("[afford.", self.service.command(argument))

    def test_the_command_is_registered_and_in_help(self):
        seen = {}
        self.sup.notifier.register = lambda name, handler: seen.__setitem__(name, handler)
        self.sup._register_commands()
        self.assertIn("afford", seen)
        self.assertIn("/afford [symbol]", self.sup.help_text())


class AutoModeTests(Harness):
    def setUp(self):
        super().setUp()
        self.broker.hedging = True
        cfg = self.sup.config.auto
        cfg.enabled, cfg.instruments = True, ["GOLD"]
        self.sup.config.management.exit_model = "three_deals"
        self.auto = self.sup.auto
        self.now = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
        for patcher in (mock.patch.object(auto_module.AutoTrader, "_now", lambda s: self.now),
                        mock.patch("tmbot.store.utcnow", lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_an_unaffordable_instrument_is_rejected_before_any_analysis(self):
        built = []
        self.sup._plan_for = lambda *a, **k: built.append(1)
        verdict = self.auto.evaluate("GOLD")
        self.assertEqual(verdict.reason, "unaffordable")
        self.assertEqual(built, [], "no plan is built for something the account cannot afford")
        text = Translator()(f"auto.reject.{verdict.reason}", **verdict.args)
        self.assertIn("the smallest lot loses", text)
        self.assertIn("over the 1.00 limit", text)
        self.assertIn("you have 100.00", text)

    def test_an_instrument_that_fits_goes_on_to_the_normal_checks(self):
        self.equity = 100_000.0
        self.service_refresh()
        self.sup._plan_for = mock.Mock(side_effect=RuntimeError("stop here"))
        self.assertEqual(self.auto.evaluate("GOLD").reason, "no_plan")     # reached the analysis

    def service_refresh(self):
        self.sup.afford.compute("GOLD", force=True)

    def test_the_scan_skips_what_does_not_fit_and_takes_the_next_that_does(self):
        self.sup.config.auto.instruments = ["GOLD", "SILVER"]
        self.add_silver(value=1.0)
        self.assertIsNotNone(self.sup.afford.unaffordable("GOLD"))
        self.assertIsNone(self.sup.afford.unaffordable("SILVER"))
        self.auto.store.set(auto_module.STATE_KEY, "on")
        scanned = []
        self.auto.scan = lambda epic: scanned.append(epic)
        for _ in range(3):
            self.auto._next_scan = 0.0
            self.auto.step()
        self.assertEqual(scanned, ["SILVER", "SILVER", "SILVER"])

    def test_unknown_instruments_are_never_skipped(self):
        self.sup.config.auto.instruments = ["GOLD", "OIL"]      # OIL has no contract value
        self.auto.store.set(auto_module.STATE_KEY, "on")
        scanned = []
        self.auto.scan = lambda epic: scanned.append(epic)
        for _ in range(2):
            self.auto._next_scan = 0.0
            self.auto.step()
        self.assertEqual(scanned, ["OIL", "OIL"])

    def test_if_nothing_fits_it_says_so_once_a_day_and_scans_nothing(self):
        self.auto.store.set(auto_module.STATE_KEY, "on")
        scanned = []
        self.auto.scan = lambda epic: scanned.append(epic)
        for _ in range(3):
            self.auto._next_scan = 0.0
            self.auto.step()
        self.assertEqual(scanned, [])
        notices = [m for _, m in self.notifier.messages if "None of the instruments fits" in m]
        self.assertEqual(len(notices), 1)
        self.assertIn("/afford", notices[0])
        self.assertTrue(any("none of the instruments fits" in e["message"]
                            for e in self.store.recent_events(20)))

    def test_the_same_notice_comes_again_the_next_day(self):
        from datetime import timedelta
        self.auto.store.set(auto_module.STATE_KEY, "on")
        self.auto._next_scan = 0.0
        self.auto.step()
        self.now += timedelta(days=1)
        self.auto._next_scan = 0.0
        self.auto.step()
        notices = [m for _, m in self.notifier.messages if "None of the instruments fits" in m]
        self.assertEqual(len(notices), 2)

    def test_nothing_about_the_risk_rules_changed(self):
        self.auto.evaluate("GOLD")
        self.assertEqual(self.sup.config.report.risk_percent, 1.0)


class ReportTests(unittest.TestCase):
    def plan(self, equity):
        broker = broker_with()
        broker.set_rules(replace(GOLD, epic="GOLD"))
        broker.account_summary = lambda: {"currency": "USD", "equity": equity}
        return ReportBuilder(broker, auto_config()).build("GOLD")

    def test_a_trade_the_risk_limit_rejects_says_what_equity_it_would_take(self):
        plan = self.plan(50.0)
        self.assertEqual(plan.sizing["verdict"], "REJECTED")
        need = plan.sizing["min_lot_money"] / 0.01
        self.assertIn(f"Skip this trade — needs equity of about {need:,.0f} USD", render_text(plan))
        full = render_full(plan)
        self.assertIn("**Minimum equity for this stop** (smallest lot, 1% risk)", full)
        self.assertIn(f"about {need:,.0f} USD — you have 50", full)
        self.assertIn("**Final Decision:** Skip this trade — needs equity of about", full)

    def test_a_trade_that_fits_still_gets_the_figure_but_no_needs_in_the_decision(self):
        plan = self.plan(10_000.0)
        full = render_full(plan)
        self.assertIn("**Minimum equity for this stop**", full)
        self.assertNotIn("needs equity of about", render_text(plan))
        self.assertNotIn("needs equity of about", full)

    def test_a_rejection_for_another_reason_does_not_claim_the_equity_is_the_problem(self):
        plan = self.plan(10_000.0)
        plan.assessment = {**plan.assessment, "verdict": "REJECTED"}
        self.assertIn("Decision: Skip this trade", render_text(plan))
        self.assertNotIn("needs equity of about", render_text(plan))

    def test_the_sizing_itself_is_unchanged(self):
        """The same plan as before: still rejected, still no lots, never forced."""
        plan = self.plan(50.0)
        self.assertEqual((plan.sizing["verdict"], plan.sizing["legs"], plan.sizing["per_leg"]),
                         ("REJECTED", 0, 0.0))
        self.assertGreater(plan.sizing["min_lot_money"], plan.sizing["max_money"])
        self.assertEqual(plan.sizing["risk_percent"], 1.0)

    def test_arabic_renders_without_missing_wording(self):
        plan = self.plan(50.0)
        for text in (render_full(plan, Translator("ar")), render_text(plan, Translator("ar"))):
            self.assertNotIn("[tr.", text)
            self.assertNotIn("[sm.", text)


if __name__ == "__main__":
    unittest.main()
