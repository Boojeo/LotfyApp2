"""The short Telegram message: side, hours per deal, decision, and /full."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tmbot.analysis import report
from tmbot.analysis.report import ReportBuilder, deal_hours, render_full, render_text
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
