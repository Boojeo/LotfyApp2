"""Automatic trade style and lot-size advice."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tmbot.analysis import style as style_module
from tmbot.analysis.report import ReportBuilder, render_text
from tmbot.broker.paper import PaperBroker
from tmbot.config import AnalysisConfig, Config, ConfigError, EpicConfig, _coerce
from tmbot.i18n import Translator
from tmbot.manage.supervisor import Supervisor
from tmbot.models import ManagedTrade, TradePlan
from tmbot.notify.base import NullNotifier
from tmbot.store import Store
from tests.helpers import RULES, candles, management, position

# 1 lot = 100 oz, so a $1 move is worth $100 a lot; lots in steps of 0.01.
GOLD = replace(RULES, value_per_point=100.0, min_deal_size=0.01, size_step=0.01)
UP = dict(end=3400.0, drift=1.5, wave=0.5)      # a clean, strong advance
FLAT = dict(end=3400.0, drift=0.0, wave=0.3)    # drifting sideways, no trend
DOWN = dict(end=3400.0, drift=-1.5, wave=0.5)
# The same advance on a slower chart: every bar spans more price.
UP_SLOW = dict(end=3400.0, drift=4.5, wave=1.5, spread=6.0)


def auto_config(**style) -> Config:
    config = Config()
    config.broker.environment = "demo"
    config.analysis = AnalysisConfig(watchlist=[EpicConfig(epic="GOLD", display="Gold")])
    config.management = management(exit_model="three_deals")
    config.llm.enabled = False
    config.news.provider = "none"
    config.report.charts = False
    config.report.intraday_refresh_hours = 0
    config.style.mode = "auto"
    for key, value in style.items():
        setattr(config.style, key, value)
    return config


def choppy(count: int = 400, price: float = 3400.0) -> list:
    """A true range: every bar undoes the last, so no trend can build."""
    from tests.helpers import BASE
    from datetime import timedelta
    from tmbot.models import Candle
    bars = []
    for index in range(count):
        close = price + (1.5 if index % 2 else -1.5)
        bars.append(Candle(ts=BASE + timedelta(minutes=15 * index),
                           open=price, high=max(price, close) + 0.5,
                           low=min(price, close) - 0.5, close=close))
    return bars


def broker_with(**charts) -> PaperBroker:
    broker = PaperBroker()
    broker.set_rules(GOLD)
    broker.set_quote("GOLD", 3400.0, 3400.2)
    for timeframe in ("M5", "M15", "H1", "H4", "D1"):
        shape = charts.get(timeframe, FLAT)
        bars = choppy() if shape is FLAT else candles(400, **shape)
        broker.set_candles("GOLD", timeframe, bars)
    return broker


class ChoiceTests(unittest.TestCase):
    def test_fixed_mode_keeps_the_configured_timeframes(self):
        config = auto_config()
        config.style.mode = "fixed"
        choice = style_module.choose(broker_with(), "GOLD", config)
        self.assertFalse(choice.automatic)
        self.assertEqual(choice.profile.entry_timeframe, config.analysis.entry_timeframe)
        self.assertEqual(choice.profile.management_timeframe,
                         config.management.management_timeframe)

    def test_a_ranging_slower_chart_keeps_the_trade_a_scalp(self):
        choice = style_module.choose(broker_with(M15=UP, H1=FLAT), "GOLD", auto_config())
        self.assertEqual(choice.profile.name, "scalp")
        self.assertEqual(choice.reason_key, "style.why_no_trend")

    def test_a_strong_trend_on_the_slower_chart_steps_up_to_intraday(self):
        choice = style_module.choose(broker_with(M15=UP, H1=UP), "GOLD", auto_config())
        self.assertEqual(choice.profile.name, "intraday")
        self.assertEqual(choice.reason_key, "style.why_trend")
        self.assertEqual(choice.profile.management_timeframe, "M15")

    def test_a_slower_trend_the_other_way_does_not_step_up(self):
        choice = style_module.choose(broker_with(M15=UP, H1=DOWN), "GOLD", auto_config())
        self.assertEqual(choice.profile.name, "scalp")
        self.assertEqual(choice.reason_key, "style.why_disagree")

    def test_swing_is_never_chosen_unless_allowed(self):
        charts = dict(M15=UP, H1=UP, H4=UP)
        self.assertEqual(
            style_module.choose(broker_with(**charts), "GOLD", auto_config()).profile.name,
            "intraday",
        )
        allowed = auto_config(allowed=["scalp", "intraday", "swing"])
        self.assertEqual(
            style_module.choose(broker_with(**charts), "GOLD", allowed).profile.name,
            "swing",
        )

    def test_missing_history_falls_back_to_the_faster_style(self):
        broker = broker_with(M15=UP)
        broker.set_candles("GOLD", "H1", [])
        choice = style_module.choose(broker, "GOLD", auto_config())
        self.assertEqual(choice.profile.name, "scalp")
        self.assertEqual(choice.reason_key, "style.why_no_data")


class SizingTests(unittest.TestCase):
    """The plan's lot advice now comes from tmbot.analysis.risk (see test_risk.py
    for the full matrix); these keep the arithmetic the report relies on."""

    def assess(self, **overrides):
        from tmbot.analysis import risk
        args = dict(equity=10_000, currency="USD", risk_percent=1.0,
                    stop_distance=5.0, rules=GOLD, legs_planned=3)
        args.update(overrides)
        return risk.assess(**args)

    def test_lots_keep_the_stop_out_within_the_risk(self):
        result = self.assess()
        # $100 risk / ($5 x $100 per lot) = 0.2 lots, / 3 deals, rounded DOWN.
        self.assertEqual(result.per_leg, 0.06)
        self.assertEqual(result.total_money, 90.0)
        self.assertEqual(result.verdict, "OK")

    def test_a_stop_too_wide_for_three_minimum_lots_advises_fewer_deals(self):
        # Used to recommend 3 x 0.01 at 1.5% risk; the limit now wins.
        result = self.assess(stop_distance=50.0)
        self.assertEqual(result.verdict, "REDUCED_LEGS")
        self.assertEqual((result.legs, result.per_leg), (2, 0.01))
        self.assertLessEqual(result.total_percent, 1.0)

    def test_no_advice_without_a_value_per_point(self):
        self.assertIsNone(self.assess(rules=RULES))


class ReportTests(unittest.TestCase):
    def build(self, **charts) -> TradePlan:
        return ReportBuilder(broker_with(**charts), auto_config()).build("GOLD")

    def test_the_plan_records_its_style_and_lot_size(self):
        plan = self.build(M15=UP, H1=UP)
        self.assertEqual(plan.style["name"], "intraday")
        self.assertEqual(plan.management_timeframe, "M15")
        self.assertEqual(plan.sizing["legs"], 3)
        self.assertGreater(plan.sizing["per_leg"], 0)

        restored = TradePlan.from_dict(plan.to_dict())
        self.assertEqual(restored.style, plan.style)
        self.assertEqual(restored.sizing, plan.sizing)

    def test_the_report_says_the_style_why_and_the_lot_size(self):
        text = render_text(self.build(M15=UP, H1=FLAT))
        self.assertIn("Style: Scalp", text)
        self.assertIn("managed on M5", text)
        self.assertIn("no strong trend on H1", text)
        self.assertIn("open 3 x", text)
        self.assertIn("loss at SL", text)

        arabic = render_text(self.build(M15=UP, H1=FLAT), Translator("ar"))
        self.assertIn("سكالبينج", arabic)
        self.assertIn("لوت", arabic)

    def test_a_scalp_stop_is_tighter_than_an_intraday_one(self):
        scalp = self.build(M15=UP, H1=FLAT, M5=UP)
        intraday = self.build(M15=UP, H1=UP_SLOW, H4=UP_SLOW)
        self.assertEqual(scalp.style["name"], "scalp")
        self.assertEqual(intraday.style["name"], "intraday")
        self.assertLess(scalp.risk, intraday.risk)
        self.assertGreaterEqual(scalp.sizing["per_leg"], intraday.sizing["per_leg"])

    def test_old_plans_without_a_style_still_load_and_render(self):
        plan = self.build()
        raw = plan.to_dict()
        raw.pop("style")
        raw.pop("sizing")
        old = TradePlan.from_dict(raw)
        self.assertEqual(old.management_timeframe, "")
        self.assertNotIn("Style:", render_text(old))


class ManagementTests(unittest.TestCase):
    def test_an_adopted_trade_keeps_its_style_chart(self):
        config = auto_config()
        broker = broker_with(M15=UP, H1=FLAT)
        store = Store(":memory:")
        supervisor = Supervisor(broker, store, config, NullNotifier())
        supervisor.start()
        broker.seed_position(position())

        supervisor.tick()

        trade = store.pending_trades()[0]
        self.assertEqual(trade.management_timeframe, "M5")
        self.assertEqual(
            ManagedTrade.from_dict(trade.to_dict()).management_timeframe, "M5"
        )

    def test_the_snapshot_reads_the_trade_chart_not_the_default(self):
        config = auto_config()
        broker = broker_with()
        broker.set_candles("GOLD", "M5", candles(300, end=3400.0, drift=0.1))
        broker.set_candles("GOLD", config.management.management_timeframe, [])
        supervisor = Supervisor(broker, Store(":memory:"), config, NullNotifier())

        snapshot = supervisor._snapshot("GOLD", "M5")

        self.assertEqual(len(snapshot.candles), 300)
        self.assertGreater(snapshot.atr, 0)


class ConfigTests(unittest.TestCase):
    def test_style_section_is_read_from_yaml(self):
        config = _coerce(Config, {"style": {"mode": "auto", "allowed": ["scalp"]}})
        self.assertEqual(config.style.mode, "auto")
        self.assertEqual(config.style.allowed, ["scalp"])

    def test_bad_style_settings_are_refused(self):
        for style in ({"mode": "fast"}, {"allowed": ["scalping"]}, {"allowed": []}):
            config = auto_config(**style)
            with self.assertRaises(ConfigError):
                config.validate(connecting=False)

    def test_default_is_unchanged_fixed_timeframes(self):
        self.assertEqual(Config().style.mode, "fixed")


if __name__ == "__main__":
    unittest.main()
