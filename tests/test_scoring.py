"""The three-part shadow analysis: what it says, and proof it decides nothing."""

from __future__ import annotations

import copy
import unittest
from datetime import timedelta

from tmbot.analysis import funnel, scoring, shadow_text
from tmbot.analysis.report import ReportBuilder, render_full, render_text
from tmbot.analysis.news import NewsProvider
from tmbot.config import Config, ConfigError
from tmbot.i18n import Translator
from tmbot.models import utcnow
from tests.test_style import DOWN, UP, FLAT, auto_config, broker_with

T = Translator()


class FakeNews(NewsProvider):
    name = "fake"

    def __init__(self, items):
        self.items = items

    def fetch(self, query, *, hours=24, limit=12):
        return list(self.items)


def headline(hours_old, title="Gold rallies as inflows surge", source="Wire"):
    stamp = (utcnow() - timedelta(hours=hours_old)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return {"title": title, "summary": "demand optimism", "source": source, "published_at": stamp}


def build(shape, mode="legacy", news=None, provider=None):
    config = auto_config()
    config.scoring.mode = mode
    builder = ReportBuilder(broker_with(M15=shape, H1=shape), config,
                            news_provider=provider or (FakeNews(news) if news is not None else None))
    return builder.build("GOLD", fundamentals=news is not None)


class ModeTests(unittest.TestCase):
    def test_legacy_is_the_default_and_computes_nothing(self):
        self.assertEqual(Config().scoring.mode, "legacy")
        plan = build(UP)
        self.assertEqual(plan.shadow, {})
        self.assertNotIn("Three-part", render_text(plan))
        self.assertNotIn("THREE-PART", render_full(plan))

    def test_an_unknown_mode_is_refused(self):
        config = auto_config()
        config.scoring.mode = "new"
        with self.assertRaises(ConfigError):
            config.validate(connecting=False)

    def test_shadow_shows_three_results_in_both_messages_and_languages(self):
        plan = build(UP, "shadow")
        for language in ("en", "ar"):
            t = Translator(language)
            short, full = render_text(plan, t), render_full(plan, t)
            for text in (short, full):
                for marker in ("[shadow.", "[sm.", "[tr."):
                    self.assertNotIn(marker, text)
            self.assertEqual(len(shadow_text.short_lines(plan.shadow, t)), 4)
        short = render_text(plan)
        self.assertIn("📈 Technical:", short)
        self.assertIn("📰 Fundamental:", short)
        self.assertIn("🧭 Overall:", short)
        self.assertIn("THREE-PART ANALYSIS", render_full(plan))

    def test_the_shadow_survives_storage(self):
        plan = build(UP, "shadow")
        from tmbot.models import TradePlan
        self.assertEqual(TradePlan.from_dict(plan.to_dict()).shadow, plan.shadow)
        self.assertEqual(TradePlan.from_dict({**plan.to_dict(), "shadow": None}).shadow, {})


class ParityTests(unittest.TestCase):
    """Shadow on or off, every existing number and decision is identical."""

    VOLATILE = ("created_at", "plan_id", "shadow")

    @classmethod
    def scrub(cls, value):
        """Drop clock-dependent fields (nested timestamps differ between two builds)."""
        if isinstance(value, dict):
            return {k: cls.scrub(v) for k, v in value.items()
                    if not (k.endswith("_at") or k in ("plan_id", "expires", "valid_until", "age"))}
        if isinstance(value, list):
            return [cls.scrub(v) for v in value]
        return value

    def same(self, shape, news=None):
        off = build(shape, "legacy", news)
        on = build(shape, "shadow", news)
        a, b = self.scrub(off.to_dict()), self.scrub(on.to_dict())
        for key in self.VOLATILE:
            a.pop(key, None), b.pop(key, None)
        self.assertEqual(a, b)
        t = Translator()
        for minimum in (0.0, 40.0, 60.0, 90.0):
            self.assertEqual(funnel.auto_blocker(off, min_strength=minimum, t=t),
                             funnel.auto_blocker(on, min_strength=minimum, t=t))
        self.assertEqual(off.confidence, on.confidence)
        self.assertEqual(off.bias, on.bias)
        self.assertEqual(off.assessment, on.assessment)
        self.assertTrue(on.shadow)
        return off, on

    def test_identical_without_news(self):
        for shape in (UP, DOWN, FLAT):
            self.same(shape)

    def test_identical_with_fresh_stale_and_mixed_news(self):
        for news in ([headline(1)], [headline(40)], [headline(1), headline(30, "Gold falls")], []):
            self.same(UP, news)
            self.same(DOWN, news)

    def test_the_short_message_only_gains_the_shadow_block(self):
        off, on = self.same(UP)
        extra = set(render_text(on).splitlines()) - set(render_text(off).splitlines())
        self.assertEqual(len(extra), 4)

    def test_the_minimum_and_auto_settings_are_not_touched(self):
        config = auto_config()
        self.assertEqual(config.auto.min_signal_strength, 60.0)
        before = copy.deepcopy(config.auto)
        ReportBuilder(broker_with(M15=UP, H1=UP), config).build("GOLD")
        self.assertEqual(config.auto, before)

    def test_a_shadow_failure_cannot_cost_the_plan(self):
        config = auto_config()
        config.scoring.mode = "shadow"
        builder = ReportBuilder(broker_with(M15=UP, H1=UP), config)
        from unittest import mock
        with mock.patch.object(scoring, "compute", side_effect=RuntimeError("boom")):
            with self.assertLogs("tmbot.analysis.report", level="ERROR"):
                plan = builder.build("GOLD")
        self.assertEqual(plan.shadow, {})
        self.assertGreater(plan.confidence, 0)


class FundamentalTests(unittest.TestCase):
    def test_no_news_is_unavailable_not_neutral_and_not_counted(self):
        shadow = build(UP, "shadow").shadow
        fund, over = shadow["fundamental"], shadow["overall"]
        self.assertEqual(fund["status"], "unavailable")
        self.assertIsNone(fund["direction"])
        self.assertIsNone(fund["strength"])
        self.assertEqual(over["basis"], "technical_only")
        self.assertEqual(over["strength"], shadow["technical"]["strength"])
        self.assertEqual(over["direction"], shadow["technical"]["direction"])
        self.assertEqual(over["weights"], {"technical": 1.0, "fundamental": 0.0})

    def test_technical_strength_does_not_depend_on_news(self):
        none = build(UP, "shadow").shadow["technical"]
        with_news = build(UP, "shadow", [headline(1), headline(2), headline(3)]).shadow["technical"]
        self.assertEqual(none["strength"], with_news["strength"])
        self.assertEqual(none["direction"], with_news["direction"])

    def test_stale_and_undated_news_is_left_out_with_a_reason(self):
        old = build(UP, "shadow", [headline(60)]).shadow["fundamental"]
        self.assertEqual((old["status"], old["why"]), ("unavailable", "stale"))
        undated = build(UP, "shadow", [{"title": "Gold surges", "source": "X"}]).shadow["fundamental"]
        self.assertEqual(undated["status"], "unavailable")
        self.assertEqual(undated["items"][0]["skipped"], "no_timestamp")

    def test_fresh_news_shows_source_time_age_and_quality(self):
        fund = build(UP, "shadow", [headline(1), headline(2), headline(3)]).shadow["fundamental"]
        self.assertEqual(fund["status"], "available")
        self.assertEqual(fund["n_used"], 3)
        item = fund["items"][0]
        self.assertEqual(item["source"], "Wire")
        self.assertTrue(item["published_at"])
        self.assertLess(item["age_hours"], 4)
        self.assertGreater(fund["quality"], 0)
        full = render_full(build(UP, "shadow", [headline(1)]))
        self.assertIn("Wire", full)
        self.assertIn("h ago", full)

    def test_thin_and_old_news_count_for_less(self):
        a = scoring.fundamental_result  # same read, different data quality
        thin = build(UP, "shadow", [headline(1)]).shadow["fundamental"]
        wide = build(UP, "shadow", [headline(1), headline(1, "More demand"), headline(2, "Inflows")]).shadow["fundamental"]
        older = build(UP, "shadow", [headline(20)]).shadow["fundamental"]
        self.assertLess(thin["quality"], wide["quality"])
        self.assertLess(older["quality"], thin["quality"])


def tech(direction, strength, score=None):
    sign = {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}[direction]
    return {"direction": direction, "strength": strength,
            "score": score if score is not None else sign * strength}


def fund(direction, strength, quality=1.0):
    return {"status": "available", "why": "", "direction": direction, "strength": strength,
            "quality": quality}


class CombinationTests(unittest.TestCase):
    def test_agreement_is_a_weighted_average(self):
        over = scoring.overall_result(tech("BULLISH", 70), fund("BULLISH", 50))
        self.assertEqual(over["agreement"], "agree")
        self.assertAlmostEqual(over["strength"], 0.7 * 70 + 0.3 * 50, places=1)
        self.assertEqual(over["direction"], "BULLISH")
        self.assertEqual(over["reliability"], "high")

    def test_contradiction_cancels_strength_and_lowers_reliability(self):
        over = scoring.overall_result(tech("BULLISH", 70), fund("BEARISH", 80))
        self.assertEqual(over["agreement"], "contradict")
        self.assertLess(over["strength"], 70 * 0.7 + 80 * 0.3)
        self.assertAlmostEqual(over["strength"], abs(0.7 * 70 - 0.3 * 80), places=1)
        self.assertEqual(over["reliability"], "reduced")

    def test_a_strong_opposing_view_can_flip_or_neutralise_the_direction(self):
        over = scoring.overall_result(tech("BULLISH", 45, 30), fund("BEARISH", 90))
        self.assertIn(over["direction"], ("BEARISH", "NEUTRAL"))

    def test_poor_quality_news_weighs_less(self):
        good = scoring.overall_result(tech("BULLISH", 70), fund("BEARISH", 80, 1.0))
        poor = scoring.overall_result(tech("BULLISH", 70), fund("BEARISH", 80, 0.2))
        self.assertGreater(poor["strength"], good["strength"])
        self.assertLess(poor["weights"]["fundamental"], good["weights"]["fundamental"])

    def test_neutral_news_is_a_small_discount_never_a_signal(self):
        over = scoring.overall_result(tech("BULLISH", 70), fund("NEUTRAL", 10, 1.0))
        self.assertEqual(over["agreement"], "partial")
        self.assertAlmostEqual(over["strength"], 70 * 0.85, places=1)
        self.assertEqual(over["direction"], "BULLISH")

    def test_which_is_stronger_is_named(self):
        self.assertEqual(scoring.overall_result(tech("BULLISH", 80), fund("BULLISH", 20))["stronger"],
                         "technical")
        self.assertEqual(scoring.overall_result(tech("BULLISH", 20), fund("BULLISH", 80))["stronger"],
                         "fundamental")
        self.assertEqual(scoring.overall_result(tech("BULLISH", 50), fund("BULLISH", 50))["stronger"],
                         "balanced")

    def test_strength_never_leaves_0_to_100(self):
        for t_dir in ("BULLISH", "BEARISH", "NEUTRAL"):
            for f_dir in ("BULLISH", "BEARISH", "NEUTRAL"):
                for strength in (0, 50, 100):
                    over = scoring.overall_result(tech(t_dir, strength), fund(f_dir, strength))
                    self.assertTrue(0 <= over["strength"] <= 100)


class GateTests(unittest.TestCase):
    def test_the_first_failing_gate_is_what_auto_blocker_says(self):
        from tests.test_auto import approved_plan
        from tmbot.models import Bias
        cases = []
        base = approved_plan()
        cases.append(base)
        for change in (
            dict(confidence=10.0), dict(advisory_only=True),
            dict(assessment={**base.assessment, "verdict": "CAUTION"}),
            dict(assessment={**base.assessment, "alignment": "CONFLICT"}),
            dict(sizing={}), dict(sizing={**base.sizing, "verdict": "REJECTED"}),
            dict(entry_zone={"valid": False}), dict(bias=Bias.BEARISH),
        ):
            plan = approved_plan()
            for key, value in change.items():
                setattr(plan, key, value)
            cases.append(plan)
        for plan in cases:
            gates = funnel.gate_results(plan, min_strength=60.0, t=T)
            failed = [g["blocker"] for g in gates if not g["passed"]]
            expected = funnel.auto_blocker(plan, min_strength=60.0, t=T)
            self.assertEqual(failed[0] if failed else funnel.PASS, expected)


if __name__ == "__main__":
    unittest.main()
