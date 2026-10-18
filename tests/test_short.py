"""The short Telegram message: side, hours per deal, decision, and /full."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tmbot.analysis import report
from tmbot.analysis.report import (
    ReportBuilder, deal_hours, detail_reasons, render_full, render_message, render_text)
from tmbot.i18n import Translator
from tests.helpers import candles
from tests.test_style import DOWN, UP, GOLD, auto_config, broker_with
from tests.test_supervisor import build_supervisor


def plan_for(shape, **overrides):
    plan = ReportBuilder(broker_with(M15=shape, H1=shape), auto_config()).build("GOLD")
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


def allowed(plan):
    plan.assessment = {**plan.assessment, "verdict": "CAUTION", "flags": []}
    plan.sizing = {**plan.sizing, "verdict": "OK", "legs": 3, "per_leg": 0.11}
    return plan


class SideTests(unittest.TestCase):
    def test_a_long_says_long_and_buy(self):
        text = render_text(allowed(plan_for(UP)), tag="[DEMO] ")
        self.assertEqual(text.splitlines()[0], "📉 [DEMO] GOLD — 🟢 LONG (BUY)")
        self.assertIn("Decision: Buy 3 x 0.11 lots", text)

    def test_a_short_says_short_and_sell(self):
        text = render_text(allowed(plan_for(DOWN)), tag="[DEMO] ")
        self.assertEqual(text.splitlines()[0], "📉 [DEMO] GOLD — 🔴 SHORT (SELL)")
        self.assertIn("Decision: Sell 3 x 0.11 lots", text)

    def test_arabic_names_the_side_too(self):
        text = render_text(allowed(plan_for(DOWN)), Translator("ar"))
        self.assertIn("بيع (SHORT)", text)
        plain = text.translate(str.maketrans("", "", "\u2066\u2067\u2068\u2069"))
        self.assertIn("القرار: بِع 3 × 0.11 لوت", plain)


class ShapeTests(unittest.TestCase):
    def test_it_is_short_in_both_languages_and_has_no_missing_wording(self):
        for shape in (UP, DOWN):
            for language in ("en", "ar"):
                text = render_text(allowed(plan_for(shape)), Translator(language))
                self.assertLess(len(text), 900, text)
                self.assertLessEqual(len(text.splitlines()), 16)
                for marker in ("[sm.", "[term.", "[tr.", "[mm."):
                    self.assertNotIn(marker, text)

    def test_it_is_far_shorter_than_the_full_report(self):
        plan = allowed(plan_for(UP))
        self.assertLess(len(render_text(plan)) * 2, len(render_full(plan)))

    def test_the_tag_is_inside_the_title_once(self):
        text = render_text(plan_for(UP), tag="[DEMO] ")
        self.assertEqual(text.count("[DEMO]"), 1)

    def test_a_rejected_trade_is_skipped_and_has_no_deal_lines(self):
        plan = plan_for(UP)
        plan.assessment = {**plan.assessment, "verdict": "REJECTED"}
        text = render_text(plan)
        self.assertIn("Decision: Skip this trade", text)
        self.assertNotIn("⏱ Deal", text)
        self.assertEqual(text.count("🎯 TP"), 3)

    def test_two_deals_close_at_tp1_and_tp3(self):
        plan = allowed(plan_for(UP))
        plan.sizing = {**plan.sizing, "legs": 2}
        text = render_text(plan)
        self.assertIn("⏱ Deal 1 → TP1", text)
        self.assertIn("⏱ Deal 2 → TP3", text)
        self.assertNotIn("TP2", text)

    def test_an_old_plan_without_a_map_still_renders(self):
        plan = allowed(plan_for(UP))
        plan.market = {}
        text = render_text(plan)
        self.assertNotIn("🧭", text)
        self.assertIn("📊 Entry", text)


class HoursTests(unittest.TestCase):
    def test_later_targets_take_longer(self):
        hours = deal_hours(plan_for(UP))
        self.assertEqual(len(hours), 3)
        self.assertTrue(hours[0] < hours[1] < hours[2])

    def test_a_weak_trend_is_slower_than_a_strong_one(self):
        strong = plan_for(UP)
        weak = plan_for(UP)
        strong.technical = {**strong.technical, "adx": 40}
        weak.technical = {**weak.technical, "adx": 10}
        # beyond one ATR a wandering market needs more candles
        self.assertGreater(deal_hours(weak)[2], deal_hours(strong)[2])

    def test_unknown_style_gives_no_hours_instead_of_a_guess(self):
        plan = plan_for(UP)
        plan.style = {}
        self.assertIsNone(deal_hours(plan))
        lines = [l for l in render_text(plan).splitlines() if l.startswith(("🎯", "⏱"))]
        self.assertTrue(lines and all(l.endswith(" -") for l in lines), lines)

    def test_under_an_hour_is_said_so(self):
        t = Translator()
        self.assertEqual(report._hours_text(0.4, t), "<1 h")
        self.assertEqual(report._hours_text(2.6, t), "~3 h")


def healthy(shape=UP):
    """A clear, aligned, low-risk plan: the case the short message is for."""
    plan = allowed(plan_for(shape))
    plan.confidence = 70.0
    plan.technical = {**plan.technical, "adx": 40}
    plan.assessment = {**plan.assessment, "alignment": "ALIGNED", "flags": [],
                       "higher_timeframe": "H4"}
    plan.sizing = {**plan.sizing, "verdict": "OK"}
    return plan


class AutoFullTests(unittest.TestCase):
    def test_a_clear_low_risk_plan_stays_short(self):
        plan = healthy()
        self.assertEqual(detail_reasons(plan, Translator()), [])
        message = render_message(plan, tag="[DEMO] ")
        self.assertEqual(message, render_text(plan, tag="[DEMO] "))
        self.assertNotIn("Full report because", message)

    def trigger(self, mutate, expected):
        plan = healthy()
        mutate(plan)
        message = render_message(plan, tag="[DEMO] ")
        self.assertIn("Full report because:", message)
        self.assertIn(expected, message.split("\n")[2])
        self.assertIn("Reversal Analysis", message)
        self.assertEqual(message.splitlines()[0], "### 📉 **[DEMO] GOLD Trade Report**")

    def test_a_weak_signal_sends_the_full_report(self):
        self.trigger(lambda p: setattr(p, "confidence", 25.0), "the signal is weak (25/100)")

    def test_no_direction_sends_the_full_report(self):
        from tmbot.models import Bias
        self.trigger(lambda p: setattr(p, "bias", Bias.NEUTRAL), "no clear direction")

    def test_a_disagreeing_higher_timeframe_sends_the_full_report(self):
        self.trigger(lambda p: setattr(p, "assessment", {**p.assessment, "alignment": "CONFLICT"}),
                     "the H4 chart disagrees")

    def test_an_unreadable_higher_timeframe_sends_the_full_report(self):
        self.trigger(lambda p: setattr(p, "assessment", {**p.assessment, "alignment": "UNCERTAIN"}),
                     "higher timeframe could not be read")

    def test_a_likely_reversal_sends_the_full_report(self):
        flags = [{"key": "timeframe_conflict", "severity": "major", "args": {"timeframe": "H4", "bias": "BEARISH"}},
                 {"key": "against_bias", "severity": "major", "args": {"bias": "BEARISH"}}]
        self.trigger(lambda p: setattr(p, "assessment", {**p.assessment, "flags": flags}),
                     "a reversal against the trade is likely")

    def test_a_likely_stop_out_sends_the_full_report(self):
        def narrow_target(plan):
            plan.tp1 = plan.reference_price + (plan.reference_price - plan.sl) * 0.2
            if plan.direction.value == "SELL":
                plan.tp1 = plan.reference_price - (plan.sl - plan.reference_price) * 0.2
        self.trigger(narrow_target, "chance of hitting the stop before TP1")

    def test_a_risk_limit_that_cannot_be_met_sends_the_full_report(self):
        self.trigger(lambda p: setattr(p, "sizing", {**p.sizing, "verdict": "REJECTED"}),
                     "the risk limit cannot be met")

    def test_an_unchecked_risk_sends_the_full_report(self):
        self.trigger(lambda p: setattr(p, "sizing", {"verdict": "UNCHECKED"}),
                     "the risk could not be checked")

    def test_several_reasons_are_listed_together(self):
        plan = healthy()
        plan.confidence = 20.0
        plan.sizing = {**plan.sizing, "verdict": "REJECTED"}
        line = render_message(plan).split("\n")[2]
        self.assertIn("the signal is weak", line)
        self.assertIn("the risk limit cannot be met", line)

    def test_a_merely_rejected_entry_stays_short(self):
        plan = healthy()
        plan.assessment = {**plan.assessment, "verdict": "REJECTED"}
        self.assertEqual(render_message(plan), render_text(plan))

    def test_the_switch_turns_it_off(self):
        plan = healthy()
        plan.confidence = 10.0
        self.assertEqual(render_message(plan, auto_full=False), render_text(plan))

    def test_arabic_gives_the_reason_too(self):
        plan = healthy()
        plan.confidence = 10.0
        text = render_message(plan, Translator("ar"))
        self.assertIn("تقرير كامل لأن", text)
        self.assertNotIn("[detail.", text)
 

class ModeCommandTests(unittest.TestCase):
    def setUp(self):
        self.supervisor, broker, self.store, _ = build_supervisor()
        broker.set_candles("GOLD", "D1", candles(5, end=3390.0))
        self.supervisor.start()

    def test_short_stops_the_long_report_and_is_remembered(self):
        self.assertTrue(self.supervisor.auto_full())
        reply = self.supervisor.short_command("")
        self.assertIn("Short messages from now on", reply)
        self.assertFalse(self.supervisor.auto_full())
        self.assertEqual(self.store.get("message_mode"), "short")

    def test_auto_brings_it_back(self):
        self.supervisor.short_command("")
        reply = self.supervisor.auto_command("")
        self.assertIn("Automatic", reply)
        self.assertTrue(self.supervisor.auto_full())

    def test_a_weak_plan_is_sent_short_after_short(self):
        plan = self.supervisor._plan_for("GOLD", force=True)
        plan.confidence = 5.0
        self.store.save_plan(plan)
        self.supervisor.short_command("")
        sent = self.supervisor._publish_plan(plan, notify=False)
        self.assertNotIn("Full report because", sent)
        self.assertNotIn("Reversal Analysis", sent)
        self.supervisor.auto_command("")
        self.assertIn("Full report because", self.supervisor._publish_plan(plan, notify=False))

    def test_the_config_default_applies_until_a_command_is_used(self):
        self.supervisor.config.report.auto_full_report = False
        self.assertFalse(self.supervisor.auto_full())
        self.supervisor.auto_command("")
        self.assertTrue(self.supervisor.auto_full())

    def test_the_commands_are_registered_with_the_notifier(self):
        seen = {}
        self.supervisor.notifier.register = lambda name, handler: seen.__setitem__(name, handler)
        self.supervisor._register_commands()
        self.assertIn("short", seen)
        self.assertIn("auto", seen)
        self.assertIn("Short messages", seen["short"](""))

    def test_the_commands_are_in_help(self):
        help_text = self.supervisor.help_text()
        self.assertIn("/short", help_text)
        self.assertIn("/auto", help_text)


class FullCommandTests(unittest.TestCase):
    def test_full_returns_every_section_and_plan_the_short_form(self):
        supervisor, broker, store, _ = build_supervisor()
        broker.set_candles("GOLD", "D1", candles(5, end=3390.0))
        supervisor.start()
        supervisor.report_command("GOLD")
        full = supervisor.full_command("GOLD")
        short = supervisor.plan_command("GOLD")
        self.assertIn("Reversal Analysis", full)
        self.assertIn("Risk Management", full)
        self.assertNotIn("Reversal Analysis", short)
        self.assertLess(len(short), len(full))

    def test_full_with_no_stored_plan_says_so(self):
        supervisor, *_ = build_supervisor()
        self.assertIn("No stored plan", supervisor.full_command("GOLD"))


if __name__ == "__main__":
    unittest.main()
