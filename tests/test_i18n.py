"""Bilingual layer: coverage, bidi safety, and that switching changes nothing stored."""

from __future__ import annotations

import unittest

from tmbot.i18n import CATALOG, LANGUAGES, Translator, catalogue_report, isolate
from tmbot.models import Bias, Direction, TradeStatus, TrendStrength
from tmbot.notify.base import NullNotifier
from tmbot.store import Store
from tests.helpers import trade


class CatalogueTests(unittest.TestCase):
    def test_every_key_has_both_languages(self):
        report = catalogue_report()
        self.assertEqual(report["missing_ar"], [], "untranslated keys")
        self.assertGreater(report["keys"], 80)

    def test_placeholders_match_across_languages(self):
        import re
        for key, entry in CATALOG.items():
            english = set(re.findall(r"\{(\w+)\}", entry["en"]))
            arabic = set(re.findall(r"\{(\w+)\}", entry["ar"]))
            self.assertEqual(english, arabic, f"{key} has mismatched placeholders")

    def test_a_missing_key_degrades_instead_of_raising(self):
        # An alert about a stop-loss must never be lost to a typo in a key.
        self.assertIn("nope.not.here", Translator("ar")("nope.not.here"))


class BidiTests(unittest.TestCase):
    def test_arabic_isolates_every_interpolated_value(self):
        text = Translator("ar")("adoption.levels", sl=3390.0, tp1=3410.0,
                                tp2=3420.0, tp3=3430.0)
        for value in (3390.0, 3410.0, 3420.0, 3430.0):
            self.assertIn(isolate(value), text,
                          "prices must be isolated or they render scrambled in RTL")

    def test_english_is_left_untouched(self):
        text = Translator("en")("adoption.levels", sl=3390.0, tp1=3410.0,
                                tp2=3420.0, tp3=3430.0)
        self.assertNotIn("⁨", text)
        self.assertIn("SL 3390.0", text)

    def test_direction_is_reported_per_language(self):
        self.assertEqual(Translator("en").direction, "ltr")
        self.assertEqual(Translator("ar").direction, "rtl")


class TermTests(unittest.TestCase):
    def test_stored_enum_values_translate_for_display_only(self):
        ar = Translator("ar")
        self.assertEqual(ar.direction_name(Direction.BUY), "شراء")
        self.assertEqual(ar.bias_name(Bias.BEARISH), "هابط")
        self.assertEqual(ar.strength_name(TrendStrength.STRONG), "قوي")
        # The enum itself is untouched -- this is what goes to SQLite.
        self.assertEqual(Direction.BUY.value, "BUY")

    def test_an_unknown_term_falls_back_to_the_raw_value(self):
        self.assertEqual(Translator("ar").term("direction", "SIDEWAYS"), "SIDEWAYS")


class LanguageSwitchTests(unittest.TestCase):
    def test_switching_language_does_not_touch_stored_records(self):
        store = Store(":memory:")
        managed = trade(tp1_done=True)
        managed.status = TradeStatus.MANAGING
        store.save_trade(managed)
        before = store.trade(managed.deal_id).to_dict()

        store.set("language", "ar")

        after = store.trade(managed.deal_id).to_dict()
        self.assertEqual(before, after)
        self.assertEqual(after["direction"], "BUY", "stored values stay English")
        self.assertEqual(after["status"], "MANAGING")

    def test_every_language_renders_the_whole_catalogue(self):
        for language in LANGUAGES:
            t = Translator(language)
            for key, entry in CATALOG.items():
                import re
                names = re.findall(r"\{(\w+)\}", entry["en"])
                rendered = t(key, **{name: "X" for name in names})
                self.assertNotIn("{", rendered, f"{key} left a placeholder in {language}")


class NoEnglishLeakTests(unittest.TestCase):
    """Guards against a future edit reintroducing a hardcoded English string."""

    ENGLISH_PHRASES = (
        "New position detected", "reached at", "stop to entry", "break-even",
        "leg", "Managing", "closed the remaining", "WAITING on", "confidence",
        "Direction bias", "trend,", "is closed at the broker", "Lost contact",
    )

    def _assert_arabic(self, text: str):
        for phrase in self.ENGLISH_PHRASES:
            self.assertNotIn(phrase, text, f"untranslated English in: {text[:120]}")

    def test_the_adoption_offer_is_fully_arabic(self):
        from tests.test_three_deals import build_supervisor
        from tests.helpers import position
        supervisor, broker, store, notifier = build_supervisor()
        supervisor.config.language = "ar"
        supervisor.t = supervisor.t.with_language("ar")
        supervisor.engine.t = supervisor.t
        supervisor.start()
        broker.seed_position(position(deal_id="deal-1", size=1.0, entry=3400.0))
        supervisor.tick()
        offer = next(text for _, text in notifier.messages if "/confirm" in text)
        self._assert_arabic(offer)

    def test_decision_reasons_are_translated_not_just_the_wrapper(self):
        from tmbot.manage.engine import TradeEngine
        from tmbot.manage.rules import evaluate
        from tmbot.broker.paper import PaperBroker
        from tmbot.config import Config
        from tmbot.store import Store
        from tests.helpers import management, snapshot, trade

        engine = TradeEngine(PaperBroker(), Store(":memory:"), Config(),
                             NullNotifier(), Translator("ar"))
        evaluation = evaluate(trade(), snapshot(3410.0), management())
        for decision in evaluation.decisions:
            rendered = engine.describe(decision)
            self._assert_arabic(rendered)
            self.assertNotEqual(rendered, decision.describe())

    def test_the_english_reason_is_preserved_for_the_audit_trail(self):
        from tmbot.manage.rules import evaluate
        from tests.helpers import management, snapshot, trade
        for decision in evaluate(trade(), snapshot(3410.0), management()).decisions:
            # `reason` is what lands in the log and the action journal.
            self.assertTrue(decision.reason.isascii(), decision.reason)


class NotifierTests(unittest.TestCase):
    def test_a_notifier_accepts_a_translator(self):
        notifier = NullNotifier()
        notifier.set_translator(Translator("ar"))  # base implementation is a no-op


if __name__ == "__main__":
    unittest.main()
