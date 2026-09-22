"""Chart rendering: it must produce a readable file, and never cost you the plan."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tmbot.analysis import chart
from tmbot.analysis.report import ReportBuilder
from tmbot.broker.paper import PaperBroker
from tmbot.config import AnalysisConfig, Config, EpicConfig
from tmbot.i18n import Translator
from tests.helpers import RULES, candles


def build_plan():
    broker = PaperBroker()
    broker.set_rules(RULES)
    broker.set_quote("GOLD", 3399.8, 3400.2)
    bars = candles(320, end=3400.0, drift=0.45, wave=7.0)
    for timeframe in ("H4", "H1", "M15"):
        broker.set_candles("GOLD", timeframe, bars)
    config = Config()
    config.broker.environment = "demo"
    config.llm.enabled = False
    config.analysis = AnalysisConfig(watchlist=[EpicConfig(epic="GOLD", display="Gold")])
    return ReportBuilder(broker, config).build("GOLD"), bars


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan, cls.bars = build_plan()

    def test_both_themes_and_languages_produce_a_real_image(self):
        with TemporaryDirectory() as directory:
            for theme in ("light", "dark"):
                for language in ("en", "ar"):
                    path = Path(directory) / f"{theme}-{language}.png"
                    result = chart.render(self.plan, self.bars, path,
                                          t=Translator(language), theme=theme)
                    self.assertTrue(result.is_file())
                    self.assertGreater(result.stat().st_size, 10_000,
                                       "suspiciously small for a rendered chart")
                    self.assertEqual(result.read_bytes()[:4], b"\x89PNG")

    def test_it_refuses_rather_than_drawing_a_meaningless_chart(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                chart.render(self.plan, self.bars[:3], Path(directory) / "x.png")

    def test_the_output_directory_is_created_on_demand(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "deeper" / "chart.png"
            self.assertTrue(chart.render(self.plan, self.bars, path).is_file())


class ArabicShapingTests(unittest.TestCase):
    def test_bidi_isolates_are_stripped_before_shaping(self):
        # python-bidi predates Unicode isolates and raises on U+2068. The
        # marks are right for Telegram and wrong here.
        text = Translator("ar")("chart.entry") + " ⁨3400.5⁩"
        shaped = chart.shape(text, rtl=True)
        for control in "⁦⁧⁨⁩‎‏":
            self.assertNotIn(control, shaped)

    def test_arabic_letters_are_joined_into_presentation_forms(self):
        shaped = chart.shape("الذهب", rtl=True)
        self.assertNotEqual(shaped, "الذهب", "text was not reshaped")
        self.assertTrue(any(0xFE70 <= ord(c) <= 0xFEFF for c in shaped))

    def test_english_passes_through_untouched(self):
        self.assertEqual(chart.shape("TP1 3418.93", rtl=False), "TP1 3418.93")

    def test_a_shaping_failure_returns_the_raw_string(self):
        # Never lose a chart to a text edge case.
        self.assertEqual(chart.shape("", rtl=True), "")


class ThemeTests(unittest.TestCase):
    def test_target_and_stop_clear_the_colourblind_floor(self):
        # The conventional TP green (#0ca30c) sits 4.1 from the SL red under
        # deuteranopia -- indistinguishable for ~1 man in 12. These do not.
        for theme in chart.THEMES.values():
            self.assertNotEqual(theme.target.lower(), "#0ca30c")
            self.assertNotEqual(theme.target, theme.stop)

    def test_both_themes_define_every_colour(self):
        for name, theme in chart.THEMES.items():
            for field_name, value in vars(theme).items():
                self.assertTrue(str(value).startswith("#"),
                                f"{name}.{field_name} is not a colour")


if __name__ == "__main__":
    unittest.main()
