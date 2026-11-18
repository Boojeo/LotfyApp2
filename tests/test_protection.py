"""Broker-side protection: verified, repaired, never duplicated, never fought."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tmbot.config import ConfigError
from tmbot.errors import PermanentError, RetryableError
from tmbot.i18n import CATALOG, Translator
from tmbot.models import Direction, TradeStatus
from tests.helpers import position
from tests.test_supervisor import build_supervisor, texts


class Harness(unittest.TestCase):
    def setUp(self):
        self.sup, self.broker, self.store, self.notifier = build_supervisor()
        self.sup.start()
        self.guard = self.sup.protection
        self.broker.seed_position(position(deal_id="deal-100001"))
        self.sup.tick()
        short = self.store.pending_trades()[0].short_id
        self.sup.confirm(short)
        self.trade = self.store.active_trades()[0]
        self.notifier.messages.clear()
        self.broker.calls.clear()
        self.guard._touched.clear()

    def live(self):
        return self.broker._positions[self.trade.deal_id]

    def tamper(self, **changes):
        self.broker._positions[self.trade.deal_id] = replace(self.live(), **changes)

    def modifies(self):
        return [c for c in self.broker.calls if c[0] == "modify_position"]

    def audit(self):
        self.guard._next = 0.0
        self.guard._last_repair.clear()
        self.guard.step({p.deal_id: p for p in self.broker.positions()})

    def say(self):
        return texts(self.notifier)


class ClassifyTests(Harness):
    def kinds(self):
        return [f.kind for f in self.guard.classify(self.trade, self.live())]

    def test_the_adopted_trade_is_protected_on_the_broker(self):
        pos = self.live()
        self.assertEqual(pos.stop_level, self.trade.sl)
        self.assertEqual(pos.profit_level, self.sup.broker_target(self.trade))
        self.assertEqual(self.kinds(), [])

    def test_each_difference_has_its_own_kind(self):
        self.tamper(stop_level=None)
        self.assertEqual(self.kinds(), ["stop_missing"])
        self.tamper(stop_level=self.trade.sl - 5)
        self.assertEqual(self.kinds(), ["stop_loose"])
        self.tamper(stop_level=self.trade.sl + 5)
        self.assertEqual(self.kinds(), ["stop_tight"])
        self.tamper(stop_level=self.trade.sl, profit_level=None)
        self.assertEqual(self.kinds(), ["target_missing"])
        target = self.sup.broker_target(self.trade)
        self.tamper(profit_level=target + 7)
        self.assertEqual(self.kinds(), ["target_different"])

    def test_a_short_mirrors_loose_and_tight(self):
        self.broker.seed_position(position(deal_id="deal-200002", direction=Direction.SELL))
        self.sup.tick()
        short_trade = [t for t in self.store.pending_trades()][0]
        self.sup.confirm(short_trade.short_id)
        short_trade = self.store.trade(short_trade.deal_id)
        live = self.broker._positions[short_trade.deal_id]
        self.broker._positions[short_trade.deal_id] = replace(live, stop_level=short_trade.sl + 5)
        self.assertEqual([f.kind for f in self.guard.classify(short_trade, self.broker._positions[short_trade.deal_id])],
                         ["stop_loose"])
        self.broker._positions[short_trade.deal_id] = replace(live, stop_level=short_trade.sl - 5)
        self.assertEqual([f.kind for f in self.guard.classify(short_trade, self.broker._positions[short_trade.deal_id])],
                         ["stop_tight"])

    def test_rounding_noise_is_not_a_difference(self):
        self.tamper(stop_level=self.trade.sl + 0.004)
        self.assertEqual(self.kinds(), [])


class RepairTests(Harness):
    def test_a_missing_stop_is_put_back_and_confirmed_on_the_server(self):
        self.tamper(stop_level=None)
        self.audit()
        self.assertEqual(self.modifies(), [("modify_position", (self.trade.deal_id,),
                                            {"stop_level": self.trade.sl, "profit_level": None})])
        self.assertEqual(self.live().stop_level, self.trade.sl)
        self.assertIn("confirmed on the broker's server", self.say())
        self.assertTrue(any(e["kind"] == "protection_repair" for e in self.store.recent_events(10)))

    def test_a_looser_stop_is_tightened_back_never_loosened(self):
        self.tamper(stop_level=self.trade.sl - 8)
        self.audit()
        self.assertEqual(self.live().stop_level, self.trade.sl)
        self.assertTrue(all(kw["stop_level"] >= self.trade.sl for _, _, kw in self.modifies()))

    def test_a_tighter_broker_stop_is_adopted_and_not_fought(self):
        self.tamper(stop_level=self.trade.sl + 4)
        self.audit()
        self.assertEqual(self.modifies(), [], "no order: the broker's stop is better")
        self.assertEqual(self.store.trade(self.trade.deal_id).sl, self.live().stop_level)
        self.assertIn("tighter stop", self.say())
        self.notifier.messages.clear()
        self.audit()
        self.assertEqual(self.modifies(), [])

    def test_a_missing_target_is_put_back(self):
        self.tamper(profit_level=None)
        self.audit()
        self.assertEqual(self.live().profit_level, self.sup.broker_target(self.trade))
        self.assertEqual(self.modifies()[0][2]["profit_level"], self.sup.broker_target(self.trade))

    def test_a_target_you_changed_by_hand_is_reported_never_overridden(self):
        self.tamper(profit_level=self.live().profit_level + 9)
        self.audit()
        self.assertEqual(self.modifies(), [])
        self.assertIn("Not changed — it may be your own edit", self.say())

    def test_the_same_report_is_not_repeated_every_check(self):
        self.tamper(profit_level=self.live().profit_level + 9)
        self.audit()
        self.notifier.messages.clear()
        self.audit()
        self.assertNotIn("your own edit", self.say())

    def test_a_clean_check_is_silent_and_sends_nothing(self):
        self.audit()
        self.assertEqual(self.notifier.messages, [])
        self.assertEqual(self.modifies(), [])

    def test_a_change_we_just_made_gets_a_grace_period(self):
        self.tamper(stop_level=None)
        self.guard.touched(self.trade)
        self.audit()
        self.assertEqual(self.modifies(), [], "give the broker time to show our own change")
        self.guard._touched[self.trade.deal_id] -= 60
        self.audit()
        self.assertEqual(len(self.modifies()), 1)

    def test_the_check_is_throttled(self):
        self.tamper(stop_level=None)
        self.guard._next = 0.0
        positions = {p.deal_id: p for p in self.broker.positions()}
        self.guard.step(positions)
        self.broker.calls.clear()
        self.tamper(stop_level=None)
        self.guard._last_repair.clear()
        self.guard.step(positions)                     # inside audit_seconds
        self.assertEqual(self.modifies(), [])

    def test_a_repair_is_never_sent_twice_in_a_row(self):
        self.tamper(stop_level=None)
        self.guard._next = 0.0
        positions = {p.deal_id: p for p in self.broker.positions()}
        self.guard.step(positions)
        self.assertEqual(len(self.modifies()), 1)
        self.tamper(stop_level=None)
        self.guard._next = 0.0
        self.guard.step(positions)                     # inside repair_cooldown_seconds
        self.assertEqual(len(self.modifies()), 1)

    def test_every_attempt_has_its_own_idempotency_key(self):
        for _ in range(2):
            self.tamper(stop_level=None)
            self.guard._touched.clear()                # past the grace period
            self.audit()
        self.assertEqual(len(self.modifies()), 2)
        self.assertEqual(self.store.get(f"prot_seq:{self.trade.deal_id}"), "2")
        done = [a for a in (self.store.action(f"{self.trade.deal_id}:repair_stop:{n}") for n in (1, 2)) if a]
        self.assertEqual([a["status"] for a in done], ["DONE", "DONE"])

    def test_a_broker_that_refuses_is_retried_a_limited_number_of_times_then_left_alone(self):
        def refuse(*a, **k):
            raise PermanentError("invalid stop", code="x")
        self.broker.modify_position = refuse
        self.tamper(stop_level=None)
        sent = []
        original = self.broker.modify_position

        def counting(*a, **k):
            sent.append(1)
            return original(*a, **k)
        self.broker.modify_position = counting
        for _ in range(6):
            self.guard._touched.clear()
            self.audit()
        self.assertEqual(len(sent), self.sup.config.protection.max_repairs)
        self.assertIn("The bot stops retrying", self.say())
        self.assertEqual(self.store.trade(self.trade.deal_id).status, TradeStatus.MANAGING,
                         "a failed repair must never stop the trade being managed")

    def test_a_deferred_repair_is_retried_without_leaving_a_claim(self):
        calls = []
        real = self.broker.modify_position

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise RetryableError("timeout")
            return real(*a, **k)
        self.broker.modify_position = flaky
        self.tamper(stop_level=None)
        self.audit()
        self.assertIn("deferred", self.say())
        self.assertEqual(self.live().stop_level, None)
        self.audit()
        self.assertEqual(self.live().stop_level, self.trade.sl)

    def test_a_repair_the_broker_accepts_but_does_not_show_is_flagged(self):
        self.broker.modify_position = lambda *a, **k: "ref-x"
        self.tamper(stop_level=None)
        self.audit()
        self.assertIn("still shows", self.say())
        self.assertEqual(self.store.get(f"prot_fail:{self.trade.deal_id}:stop"), "1")

    def test_paused_means_alert_only(self):
        self.sup.set_paused(True)
        self.notifier.messages.clear()
        self.tamper(stop_level=None)
        self.audit()
        self.assertEqual(self.modifies(), [])
        self.assertIn("Trading is paused, so nothing was changed", self.say())

    def test_dry_run_means_alert_only(self):
        self.sup.config.dry_run = True
        self.tamper(stop_level=None)
        self.audit()
        self.assertEqual(self.modifies(), [])
        self.assertIn("Nothing was sent", self.say())

    def test_only_managed_trades_are_audited(self):
        self.broker.seed_position(position(deal_id="deal-300003", direction=Direction.SELL))
        self.sup.tick()
        pending = self.store.pending_trades()[0]
        self.broker._positions[pending.deal_id] = replace(
            self.broker._positions[pending.deal_id], stop_level=None)
        self.broker.calls.clear()
        self.audit()
        self.assertEqual([c for c in self.modifies() if c[1][0] == pending.deal_id], [])

    def test_disabled_in_the_config_does_nothing(self):
        self.sup.config.protection.enabled = False
        self.tamper(stop_level=None)
        self.audit()
        self.assertEqual(self.modifies(), [])
        self.assertEqual(self.guard.full_audit("startup"), "")


class ReconnectTests(Harness):
    def test_start_up_verifies_every_trade_before_anything_else(self):
        text = self.guard.full_audit("startup")
        self.assertIn("verified at start-up: 1 trade(s) re-read from the broker", text)
        self.assertIn("are confirmed on the broker's server", text)
        self.assertEqual(self.modifies(), [])

    def test_after_a_reconnect_the_broker_is_read_and_a_lost_stop_is_repaired_first(self):
        self.sup._enter_degraded("internet down")
        self.notifier.messages.clear()
        self.tamper(stop_level=None)              # something removed it while we were away
        self.sup.tick()
        message = self.say()
        self.assertIn("verified after the reconnect", message)
        self.assertIn("put back", message)
        self.assertEqual(self.live().stop_level, self.trade.sl)
        self.assertFalse(self.sup._degraded)

    def test_a_clean_reconnect_still_says_it_was_checked(self):
        self.sup._enter_degraded("internet down")
        self.notifier.messages.clear()
        self.sup.tick()
        self.assertIn("verified after the reconnect", self.say())
        self.assertIn("confirmed on the broker's server", self.say())

    def test_a_trade_that_closed_while_we_were_away_is_reported_not_repaired(self):
        self.broker.close_position(self.trade.deal_id)
        text = self.guard.full_audit("reconnect")
        self.assertIn("no longer open at the broker", text)

    def test_an_unreadable_broker_says_the_server_side_protection_is_still_there(self):
        def down():
            raise RetryableError("offline")
        self.broker.positions = down
        text = self.guard.full_audit("reconnect")
        self.assertIn("still active", text)
        self.assertIn("cannot be read", text)

    def test_the_audit_syncs_the_trades_view_of_the_broker(self):
        self.tamper(size=0.5, stop_level=self.trade.sl)
        self.guard.full_audit("manual")
        self.assertEqual(self.store.trade(self.trade.deal_id).remaining_size, 0.5)

    def test_no_trades_means_no_message(self):
        self.broker.close_position(self.trade.deal_id)
        self.sup.tick()
        self.assertEqual(self.guard.full_audit("startup"), "")


class ReportTests(Harness):
    def test_the_report_separates_broker_side_from_app_dependent(self):
        text = self.guard.command("")
        self.assertIn("ON THE BROKER'S SERVER", text)
        self.assertIn("NEEDS THE APP RUNNING", text)
        self.assertLess(text.index("ON THE BROKER'S SERVER"), text.index("NEEDS THE APP RUNNING"))
        self.assertIn("still active if the app, this computer or the internet goes down", text)
        self.assertIn("✅ confirmed", text)
        self.assertIn(f"Stop {self.trade.sl:g}", text)
        self.assertIn("Stop to entry when", text)
        self.assertIn("Trailing stop after", text)
        self.assertIn("If the app is off these simply do not happen", text)

    def test_a_losing_position_shows_what_it_risks_and_a_winning_one_what_it_locks(self):
        self.assertIn("risks -", self.guard.command(""))
        self.tamper(stop_level=self.trade.entry_price + 4)
        self.trade.sl = self.trade.entry_price + 4
        self.store.save_trade(self.trade)
        text = self.guard.command("")
        self.assertIn("locks in", text)
        self.assertIn("+", text.split("Stop ")[1].split("\n")[0])

    def test_a_difference_is_marked(self):
        self.tamper(stop_level=None)
        self.assertIn("❌ differs from the plan", self.guard.command(""))

    def test_the_money_at_the_stop_is_checked_against_the_risk_limit(self):
        from tmbot.models import MarketRules
        from tests.helpers import RULES
        self.broker.set_rules(replace(RULES, value_per_point=100.0))
        text = self.guard.command("")
        self.assertIn("Loss if the stop is hit: -601.00 per deal", text)     # 1.0 lot, 6.01 away
        self.assertIn("OVER the risk limit (not changed by the bot)", text)
        self.assertIn("risks -1.00R = -601.00", text)
        self.broker._positions[self.trade.deal_id] = replace(self.live(), size=0.01)
        self.trade.remaining_size = 0.01
        self.store.save_trade(self.trade)
        small = self.guard.command("")
        self.assertIn("Loss if the stop is hit: -6.01 — within the risk limit", small)

    def test_without_a_contract_value_it_says_the_money_cannot_be_worked_out(self):
        text = self.guard.command("")           # the test rules publish no contract value
        self.assertIn("publishes no contract value", text)
        self.assertNotIn("= -", text)

    def test_the_atr_levels_are_shown_as_reference_only(self):
        text = self.guard.command("")
        self.assertIn("ATR reference (not applied)", text)

    def test_verify_runs_the_full_check_and_an_unknown_id_is_named(self):
        self.assertIn("verified on request", self.guard.command("verify"))
        self.assertIn("999999", self.guard.command("999999"))

    def test_nothing_open_says_so(self):
        self.broker.close_position(self.trade.deal_id)
        self.sup.tick()
        self.assertEqual(self.guard.command(""), "No managed trades to check.")

    def test_the_command_is_registered_and_in_help(self):
        seen = {}
        self.sup.notifier.register = lambda name, handler: seen.__setitem__(name, handler)
        self.sup._register_commands()
        self.assertIn("protect", seen)
        self.assertIn("/protect", self.sup.help_text())

    def test_arabic_has_every_string_and_renders_whole(self):
        for key, entry in CATALOG.items():
            if key.startswith("prot."):
                self.assertTrue(entry.get("ar"), key)
        self.sup.t = Translator("ar")
        text = self.guard.command("")
        self.assertNotIn("[prot.", text)
        self.tamper(stop_level=None)
        self.audit()
        self.assertNotIn("[prot.", self.say())


class ConfigTests(unittest.TestCase):
    def config(self, **kw):
        from tmbot.config import Config
        c = Config()
        c.broker.environment = "demo"
        c.broker.api_key = c.broker.identifier = c.broker.password = "x"
        for name, value in kw.items():
            section, field = name.split("__")
            setattr(getattr(c, section), field, value)
        return c

    def test_defaults_are_safe_and_valid(self):
        c = self.config()
        c.validate()
        self.assertTrue(c.protection.enabled)
        self.assertEqual((c.protection.audit_seconds, c.protection.max_repairs), (2.5, 3))

    def test_bad_values_are_named(self):
        for bad in ({"protection__audit_seconds": 0}, {"protection__grace_seconds": -1},
                    {"protection__max_repairs": 0}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                self.config(**bad).validate()

    def test_the_example_config_loads(self):
        import yaml
        from tmbot.config import Config, _coerce
        raw = yaml.safe_load(open("config.example.yaml"))
        loaded = _coerce(Config, raw)
        self.assertTrue(loaded.protection.enabled)
        self.assertTrue(loaded.exit_advice.enabled)


if __name__ == "__main__":
    unittest.main()
