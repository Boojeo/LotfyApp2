"""Exit advice: shown, never executed by itself, applied only when you confirm."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from tmbot.analysis import exit_advice, odds
from tmbot.analysis.exit_advice import CLOSE, HOLD, MOVE_STOP, PARTIAL_CLOSE, Advice, TargetView
from tmbot.errors import PermanentError
from tmbot.i18n import CATALOG, Translator
from tmbot.manage import advisor as advisor_module
from tmbot.models import Direction, Stage, TradeStatus, utcnow
from tests.helpers import position
from tests.test_supervisor import build_supervisor, texts


def make_advice(action=CLOSE, deal_id="", **kw):
    advice = Advice(deal_id=deal_id, action=action, r_now=2.2, r_stop=0.0,
                    reasons=[("close_ev", {"stage": "TP3", "ev": "+0.78", "now": "+2.20",
                                           "samples": 200}),
                             ("evidence", {"strength": "MODERATE", "adx": "28",
                                           "momentum": "fading", "agreeing": 1, "required": 2,
                                           "nearest": "2.5"})],
                    targets=[TargetView("TP3", 3430.0, 2.5, 3.0, 200, 0.15, 0.7, 0.1, 0.22, 0.78)],
                    best_stage="TP3", own_stage="TP3", tested=True, samples=200, horizon=96)
    for key, value in kw.items():
        setattr(advice, key, value)
    return advice


class Harness(unittest.TestCase):
    exit_model = "three_deals"

    def setUp(self):
        self.sup, self.broker, self.store, self.notifier = build_supervisor(
            exit_model=self.exit_model)
        self.sup.start()
        self.advisor = self.sup.advisor
        for number in (1, 2, 3):
            self.broker.seed_position(position(deal_id=f"deal-4000{number}0", size=0.1))
        self.sup.tick()
        self.sup.confirm(self.store.pending_trades()[0].short_id)
        self.trades = sorted(self.store.active_trades(), key=lambda t: t.leg_index)
        self.runner = self.trades[-1]
        self.notifier.messages.clear()
        self.broker.calls.clear()

    def calls(self, name):
        return [c for c in self.broker.calls if c[0] == name]

    def say(self):
        return texts(self.notifier)

    def store_advice(self, trade, advice, *, age=0):
        advice.deal_id = trade.deal_id
        self.store.set(f"advice:{trade.deal_id}", json.dumps({
            "id": "abc12345", "at": (utcnow() - timedelta(minutes=age)).isoformat(),
            "advice": advice.to_dict()}))

    def force(self, advice):
        """Make the advisor compute exactly this advice."""
        self.advisor.compute = lambda trade, snapshot, signal=None: replace(
            advice, deal_id=trade.deal_id)


class RunnerSetupTests(Harness):
    def test_a_three_deal_basket_is_adopted(self):
        self.assertEqual(len(self.trades), 3)
        self.assertEqual([t.leg_target for t in self.trades], [Stage.TP1, Stage.TP2, Stage.TP3])
        self.assertTrue(self.runner.is_runner)


class NeverActsAloneTests(Harness):
    def test_advice_every_cycle_never_touches_the_broker(self):
        self.force(make_advice(CLOSE))
        snapshot = self.sup._snapshot(self.runner.epic, self.runner.management_timeframe)
        evaluation = mock.Mock(reversal=None)
        for _ in range(5):
            self.advisor.update(self.runner, snapshot, evaluation)
        self.assertEqual(self.calls("close_position"), [])
        self.assertEqual(self.calls("modify_position"), [])
        self.assertEqual(self.store.trade(self.runner.deal_id).status, TradeStatus.MANAGING)

    def test_a_real_cycle_computes_advice_with_tested_odds_and_keeps_it(self):
        self.sup.tick()
        saved = json.loads(self.store.get(f"advice:{self.runner.deal_id}"))
        advice = saved["advice"]
        self.assertIn(advice["action"], (HOLD, CLOSE, MOVE_STOP, PARTIAL_CLOSE))
        self.assertTrue(advice["targets"])
        for target in advice["targets"]:
            if target["p"] is not None:
                self.assertGreaterEqual(target["samples"], self.sup.config.exit_advice.min_samples)

    def test_disabled_in_the_config_nothing_is_computed(self):
        self.sup.config.exit_advice.enabled = False
        self.sup.tick()
        self.assertIsNone(self.store.get(f"advice:{self.runner.deal_id}"))

    def test_a_failure_in_the_advisor_never_stops_management(self):
        self.advisor.compute = mock.Mock(side_effect=RuntimeError("boom"))
        self.sup.tick()
        self.assertEqual(self.store.trade(self.runner.deal_id).status, TradeStatus.MANAGING)


class MessageTests(Harness):
    def update(self, advice, trade=None):
        self.force(advice)
        trade = trade or self.runner
        snapshot = self.sup._snapshot(trade.epic, trade.management_timeframe)
        self.advisor.update(trade, snapshot, mock.Mock(reversal=None))

    def test_a_recommendation_to_act_is_messaged_with_reasons_and_options(self):
        self.update(make_advice(CLOSE))
        text = self.say()
        sid = self.runner.short_id
        self.assertIn("Recommendation: CLOSE", text)
        self.assertIn("Why:", text)
        self.assertIn("holding to TP3 is worth +0.78R on average (200 test trades)", text)
        self.assertIn("evidence:", text)
        self.assertIn("reached before the stop in 15% of test trades (10–22%)", text)
        self.assertIn("Most realistic target by tested value: TP3", text)
        self.assertIn(f"/do {sid}", text)
        self.assertIn(f"/skip {sid}", text)
        self.assertIn(f"/targets {sid} 2", text)
        self.assertIn(f"/targets {sid} 3", text)
        self.assertIn("not a forecast", text)
        self.assertIn("Nothing happens unless you confirm", text)

    def test_the_same_advice_is_not_repeated(self):
        self.update(make_advice(CLOSE))
        self.notifier.messages.clear()
        self.update(make_advice(CLOSE))
        self.assertEqual(self.notifier.messages, [])

    def test_hold_is_silent_until_something_to_act_on_appears(self):
        self.update(make_advice(HOLD))
        self.assertEqual(self.notifier.messages, [])

    def test_going_back_to_hold_is_told_once(self):
        self.update(make_advice(CLOSE))
        self.sup.config.exit_advice.advice_cooldown_minutes = 0
        self.notifier.messages.clear()
        self.update(make_advice(HOLD))
        self.assertIn("Recommendation: HOLD", self.say())
        self.notifier.messages.clear()
        self.update(make_advice(HOLD))
        self.assertEqual(self.notifier.messages, [])

    def test_changes_inside_the_cooldown_are_held_back(self):
        self.update(make_advice(CLOSE))
        self.notifier.messages.clear()
        self.update(make_advice(MOVE_STOP, stop_level=3411.0))
        self.assertEqual(self.notifier.messages, [], "inside advice_cooldown_minutes")

    def test_a_move_stop_and_a_partial_close_say_what_exactly(self):
        self.sup.config.exit_advice.advice_cooldown_minutes = 0
        self.update(make_advice(MOVE_STOP, stop_level=3411.5))
        self.assertIn("New stop: 3411.5", self.say())
        self.update(make_advice(PARTIAL_CLOSE, size=0.5))
        self.assertIn("Close 0.5 lots", self.say())

    def test_without_odds_the_message_says_there_is_no_number(self):
        advice = make_advice(HOLD, tested=False,
                             targets=[TargetView("TP3", 3430.0, 2.5, 3.0, 12)],
                             reasons=[("hold", {"r": "+0.10"}),
                                      ("evidence", {"strength": "WEAK", "adx": "15",
                                                    "momentum": "steady", "agreeing": 0,
                                                    "required": 2, "nearest": "2.5"}),
                                      ("no_odds", {"samples": 12, "minimum": 30})])
        text = self.advisor.render(self.runner, advice)
        self.assertIn("no tested odds: only 12 test trades", text)
        self.assertIn("no tested odds (not enough history)", text)
        self.assertNotIn("%", text.split("TP3 3430.0")[1].split("\n")[0])

    def test_a_skipped_advice_is_not_sent_again_until_it_changes(self):
        self.update(make_advice(CLOSE))
        self.advisor.skip_command(self.runner.short_id)
        self.sup.config.exit_advice.advice_cooldown_minutes = 0
        self.notifier.messages.clear()
        self.update(make_advice(CLOSE))
        self.assertEqual(self.notifier.messages, [])
        self.update(make_advice(MOVE_STOP, stop_level=3411.0))
        self.assertIn("MOVE STOP LOSS", self.say())


class ExitCommandTests(Harness):
    def test_exit_shows_advice_for_every_managed_deal(self):
        text = self.advisor.exit_command("")
        self.assertEqual(text.count("🧭 Exit advice"), 3)
        for trade in self.trades:
            self.assertIn(f"[{trade.short_id}]", text)

    def test_exit_for_one_deal_and_unknown_and_empty(self):
        text = self.advisor.exit_command(self.runner.short_id)
        self.assertEqual(text.count("🧭 Exit advice"), 1)
        self.assertIn("999999", self.advisor.exit_command("999999"))
        for trade in self.trades:
            self.broker.close_position(trade.deal_id)
        self.sup.tick()
        self.assertEqual(self.advisor.exit_command(""), "No managed trades.")

    def test_exit_saves_the_advice_so_do_can_confirm_it(self):
        self.advisor.exit_command(self.runner.short_id)
        self.assertIsNotNone(self.advisor._load(self.runner))

    def test_the_commands_are_registered_and_in_help(self):
        seen = {}
        self.sup.notifier.register = lambda name, handler: seen.__setitem__(name, handler)
        self.sup._register_commands()
        for name in ("exit", "do", "skip", "targets"):
            self.assertIn(name, seen)
        for line in ("/exit", "/do <id>", "/skip <id>", "/targets <id> 2|3"):
            self.assertIn(line, self.sup.help_text())


class DoCommandTests(Harness):
    def confirm(self, advice, trade=None, age=0):
        trade = trade or self.runner
        self.force(advice)
        self.store_advice(trade, advice, age=age)
        return self.advisor.do_command(trade.short_id)

    def test_a_confirmed_close_goes_through_the_normal_close_path_once(self):
        text = self.confirm(make_advice(CLOSE))
        self.assertEqual(len(self.calls("close_position")), 1)
        self.assertEqual(self.calls("close_position")[0][1][0], self.runner.deal_id)
        self.assertEqual(self.store.trade(self.runner.deal_id).status, TradeStatus.CLOSED)
        self.assertIn("closed as advised (confirmed by you)", text)
        # one confirmation, one action: the deal is gone, a second /do finds nothing
        self.assertIn("No trade matching", self.advisor.do_command(self.runner.short_id))
        self.assertEqual(len(self.calls("close_position")), 1)
        self.assertEqual(self.store.get(f"advice:{self.runner.deal_id}"), "")

    def test_only_the_named_deal_is_touched(self):
        self.confirm(make_advice(CLOSE))
        self.assertEqual([t.status for t in self.store.active_trades()],
                         [TradeStatus.MANAGING, TradeStatus.MANAGING])

    def test_a_confirmed_stop_move_updates_the_broker_and_the_trade(self):
        level = self.runner.entry_price - 2.0
        text = self.confirm(make_advice(MOVE_STOP, stop_level=level))
        self.assertEqual(self.broker._positions[self.runner.deal_id].stop_level, level)
        self.assertEqual(self.store.trade(self.runner.deal_id).sl, level)
        self.assertEqual(self.calls("close_position"), [])
        self.assertIn(f"stop moved to {level}", text)

    def test_an_old_advice_is_refused(self):
        text = self.confirm(make_advice(CLOSE), age=11)
        self.assertIn("more than 10 minutes old", text)
        self.assertEqual(self.calls("close_position"), [])

    def test_advice_that_changed_since_you_saw_it_is_not_executed(self):
        self.store_advice(self.runner, make_advice(CLOSE))
        self.force(make_advice(HOLD))
        text = self.advisor.do_command(self.runner.short_id)
        self.assertIn("now HOLD, not CLOSE", text)
        self.assertEqual(self.calls("close_position"), [])
        self.assertEqual(self.store.trade(self.runner.deal_id).status, TradeStatus.MANAGING)

    def test_a_moved_stop_level_counts_as_changed(self):
        self.store_advice(self.runner, make_advice(MOVE_STOP, stop_level=3395.0))
        self.force(make_advice(MOVE_STOP, stop_level=3396.0))
        self.assertIn("now MOVE STOP LOSS", self.advisor.do_command(self.runner.short_id))
        self.assertEqual(self.calls("modify_position"), [])

    def test_hold_has_nothing_to_apply(self):
        self.store_advice(self.runner, make_advice(HOLD))
        self.assertIn("HOLD: there is nothing to apply", self.advisor.do_command(self.runner.short_id))

    def test_no_advice_and_unknown_ids(self):
        self.assertIn("No advice is waiting", self.advisor.do_command(self.runner.short_id))
        self.assertIn("999999", self.advisor.do_command("999999"))

    def test_the_broker_refusing_a_close_is_reported(self):
        def refuse(*a, **k):
            raise PermanentError("market closed", code="x")
        self.broker.close_position = refuse
        text = self.confirm(make_advice(CLOSE))
        self.assertTrue(text)
        self.assertNotEqual(self.store.trade(self.runner.deal_id).status, TradeStatus.CLOSED)

    def test_the_confirmed_change_is_logged(self):
        self.confirm(make_advice(CLOSE))
        self.assertTrue(any(e["kind"] == "advice_applied" for e in self.store.recent_events(20)))


class PartialCloseTests(unittest.TestCase):
    def test_a_confirmed_partial_close_closes_only_that_share(self):
        sup, broker, store, notifier = build_supervisor()      # the partial-close model
        sup.start()
        broker.seed_position(position(deal_id="deal-500001", size=1.0))
        sup.tick()
        sup.confirm(store.pending_trades()[0].short_id)
        trade = store.active_trades()[0]
        advice = make_advice(PARTIAL_CLOSE, size=0.5)
        sup.advisor.compute = lambda t, s, signal=None: replace(advice, deal_id=t.deal_id)
        store.set(f"advice:{trade.deal_id}", json.dumps({
            "id": "p1", "at": utcnow().isoformat(),
            "advice": replace(advice, deal_id=trade.deal_id).to_dict()}))
        broker.calls.clear()
        sup.advisor.do_command(trade.short_id)
        closes = [c for c in broker.calls if c[0] == "close_position"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][2]["size"], 0.5)
        self.assertAlmostEqual(broker._positions[trade.deal_id].size, 0.5)


class TargetsTests(Harness):
    def test_t1_t2_only_pulls_the_final_target_in_on_the_broker(self):
        text = self.advisor.targets_command(f"{self.trades[0].short_id} 2")
        runner = self.store.trade(self.runner.deal_id)
        self.assertEqual(runner.tp3, self.runner.tp2)
        self.assertEqual(runner.tp3_plan, self.runner.tp3)
        self.assertEqual(self.broker._positions[self.runner.deal_id].profit_level, self.runner.tp2)
        self.assertEqual(runner.tp3_extensions, self.sup.config.management.tp3_max_extensions)
        self.assertIn("T1–T2 only", text)
        self.assertIn("set on the broker's server", text)

    def test_the_other_deals_are_left_exactly_as_they_were(self):
        before = {t.deal_id: (t.tp1, t.tp2, t.tp3, self.broker._positions[t.deal_id].profit_level)
                  for t in self.trades[:2]}
        self.advisor.targets_command(f"{self.runner.short_id} 2")
        for trade in self.trades[:2]:
            after = (trade.tp1, trade.tp2, trade.tp3,
                     self.broker._positions[trade.deal_id].profit_level)
            self.assertEqual(after, before[trade.deal_id])

    def test_back_to_t1_t2_t3_restores_the_plans_target(self):
        self.advisor.targets_command(f"{self.runner.short_id} 2")
        text = self.advisor.targets_command(f"{self.runner.short_id} 3")
        runner = self.store.trade(self.runner.deal_id)
        self.assertEqual(runner.tp3, self.runner.tp3)
        self.assertEqual(runner.tp3_plan, 0.0)
        self.assertEqual(runner.tp3_extensions, 0)
        self.assertEqual(self.broker._positions[self.runner.deal_id].profit_level, self.runner.tp3)
        self.assertIn("T1–T2–T3 again", text)

    def test_asking_for_what_is_already_set_does_nothing(self):
        self.assertIn("already set to T1–T2–T3",
                      self.advisor.targets_command(f"{self.runner.short_id} 3"))
        self.advisor.targets_command(f"{self.runner.short_id} 2")
        self.broker.calls.clear()
        self.assertIn("already set to T1–T2", self.advisor.targets_command(f"{self.runner.short_id} 2"))
        self.assertEqual(self.calls("modify_position"), [])

    def test_a_target_the_price_has_already_passed_is_refused(self):
        self.broker.set_quote("GOLD", self.runner.tp2 + 1, self.runner.tp2 + 1.2)
        text = self.advisor.targets_command(f"{self.runner.short_id} 2")
        self.assertIn("already at or past", text)
        self.assertEqual(self.store.trade(self.runner.deal_id).tp3, self.runner.tp3)
        self.assertEqual(self.calls("modify_position"), [])

    def test_a_refused_target_keeps_the_trade_managed_and_the_plan_unchanged(self):
        def refuse(*a, **k):
            raise PermanentError("invalid target", code="x")
        self.broker.modify_position = refuse
        text = self.advisor.targets_command(f"{self.runner.short_id} 2")
        runner = self.store.trade(self.runner.deal_id)
        self.assertEqual(runner.tp3, self.runner.tp3)
        self.assertEqual(runner.status, TradeStatus.MANAGING)
        self.assertEqual(runner.tp3_extensions, 0)
        self.assertIn("did not accept", text)

    def test_the_protection_audit_agrees_with_the_new_target(self):
        self.advisor.targets_command(f"{self.runner.short_id} 2")
        trade = self.store.trade(self.runner.deal_id)
        self.assertEqual(self.sup.protection.classify(trade, self.broker._positions[trade.deal_id]), [])

    def test_usage_and_unknown_ids(self):
        for bad in ("", "abc", f"{self.runner.short_id}", f"{self.runner.short_id} 4"):
            self.assertIn("/targets <id> 2", self.advisor.targets_command(bad))
        self.assertIn("999999", self.advisor.targets_command("999999 2"))

    def test_any_deal_of_the_basket_can_name_it(self):
        self.advisor.targets_command(f"{self.trades[0].short_id} 2")
        self.assertEqual(self.store.trade(self.runner.deal_id).tp3, self.runner.tp2)

    def test_the_final_target_change_is_logged(self):
        self.advisor.targets_command(f"{self.runner.short_id} 2")
        self.assertTrue(any(e["kind"] == "targets" for e in self.store.recent_events(20)))

    def test_the_extension_cannot_move_a_target_that_was_pulled_in(self):
        from tmbot.manage.rules import evaluate
        self.advisor.targets_command(f"{self.runner.short_id} 2")
        runner = self.store.trade(self.runner.deal_id)
        from tests.helpers import snapshot
        from tmbot.models import TrendStrength
        snap = snapshot(runner.tp3 + 1.0, atr=4.0, strength=TrendStrength.STRONG)
        result = evaluate(runner, snap, self.sup.config.management)
        self.assertFalse([d for d in result.decisions if d.kind.value == "SET_TARGET"])


class OddsCacheTests(Harness):
    def test_the_history_test_is_reused_not_rerun_every_cycle(self):
        snapshot = self.sup._snapshot(self.runner.epic, self.runner.management_timeframe)
        with mock.patch.object(advisor_module.odds_module, "reach_rates",
                               wraps=advisor_module.odds_module.reach_rates) as spy:
            self.advisor._odds_for(self.runner, snapshot)
            self.advisor._odds_for(self.runner, snapshot)
            self.advisor._odds_for(self.runner, snapshot)
        self.assertEqual(spy.call_count, 1)

    def test_the_test_uses_the_configured_horizon_and_minimum(self):
        snapshot = self.sup._snapshot(self.runner.epic, self.runner.management_timeframe)
        self.sup.config.exit_advice.horizon_bars = 30
        with mock.patch.object(advisor_module.odds_module, "reach_rates",
                               wraps=advisor_module.odds_module.reach_rates) as spy:
            self.advisor._odds_for(self.runner, snapshot)
        self.assertEqual(spy.call_args.kwargs["horizon"], 30)
        self.assertEqual(spy.call_args.kwargs["min_samples"], 30)


class TextTests(Harness):
    def test_arabic_has_every_string_and_renders_whole(self):
        for key, entry in CATALOG.items():
            if key.startswith(("adv.", "ez.", "reason.advice")):
                self.assertTrue(entry.get("ar"), key)
        self.sup.t = Translator("ar")
        text = self.advisor.render(self.runner, make_advice(CLOSE))
        self.assertNotIn("[adv.", text)
        text = self.advisor.exit_command("")
        self.assertNotIn("[adv.", text)

    def test_every_reason_the_advisor_can_give_has_wording(self):
        for key in ("close_reversal", "close_ev", "realistic_target", "move_stop",
                    "stop_not_improved", "hold", "evidence", "no_odds"):
            self.assertIn(f"adv.reason.{key}", CATALOG)

    def test_every_entry_range_reason_has_wording(self):
        for key in ("no_acceptable_entry", "stop_touched", "tp1_passed", "outside_range",
                    "risk_over_limit", "reward_risk_low", "expired", "bias_changed"):
            self.assertIn(f"ez.check.{key}", CATALOG)
            self.assertTrue(CATALOG[f"ez.check.{key}"].get("ar"))


class ConfigTests(unittest.TestCase):
    def test_bad_advice_settings_are_named(self):
        from tmbot.config import Config, ConfigError
        for bad in ({"history_bars": 50}, {"horizon_bars": 0}, {"min_samples": 5},
                    {"partial_fraction": 1.0}, {"partial_fraction": 0},
                    {"advice_cooldown_minutes": -1}, {"stale_minutes": 0}):
            c = Config()
            c.broker.environment = "demo"
            c.broker.api_key = c.broker.identifier = c.broker.password = "x"
            for key, value in bad.items():
                setattr(c.exit_advice, key, value)
            with self.assertRaises(ConfigError, msg=str(bad)):
                c.validate()

    def test_defaults(self):
        from tmbot.config import Config
        c = Config()
        self.assertEqual((c.exit_advice.history_bars, c.exit_advice.horizon_bars,
                          c.exit_advice.min_samples), (1000, 96, 30))


if __name__ == "__main__":
    unittest.main()
