"""Automatic trading: every guard in front of an order, and what happens after."""

from __future__ import annotations

import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from tmbot.analysis import risk as risk_module
from tmbot.analysis.report import ReportBuilder
from tmbot.config import RISK_CHOICES, ConfigError
from tmbot.errors import PermanentError, RetryableError
from tmbot.i18n import CATALOG, Translator
from tmbot.manage import auto as auto_module
from tmbot.models import Bias, Direction, MarketRules, TradeStatus
from tests.helpers import RULES, position
from tests.test_style import GOLD, UP, auto_config, broker_with
from tests.test_supervisor import build_supervisor, texts

UTC = timezone.utc
TUESDAY_10 = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)


def approved_plan(epic="GOLD", direction=Direction.BUY):
    """A clear, aligned, strong, low-risk plan the gates should accept."""
    plan = ReportBuilder(broker_with(M15=UP, H1=UP), auto_config()).build("GOLD")
    plan.epic = epic
    plan.direction = direction
    plan.bias = Bias.BULLISH if direction is Direction.BUY else Bias.BEARISH
    plan.advisory_only = False
    plan.confidence = 72.0
    plan.technical = {**plan.technical, "adx": 40}
    plan.assessment = {"verdict": "APPROVED", "verdict_key": "verdict.approved",
                       "alignment": "ALIGNED", "higher_timeframe": "H4", "flags": [],
                       "quality": "GOOD"}
    sign = direction.sign
    plan.reference_price = 3400.0
    plan.sl = 3400.0 - 3.0 * sign
    plan.tp1, plan.tp2, plan.tp3 = (3400.0 + sign * 3.0, 3400.0 + sign * 6.0,
                                    3400.0 + sign * 10.0)
    plan.atr = 2.0
    plan.market = {}
    plan.sizing = {"verdict": "OK", "legs": 3, "per_leg": 0.11, "risk_percent": 1.0,
                   "max_money": 100.0, "total_money": 99.0, "currency": "USD",
                   "min_lot_money": 3.0, "targets": ["TP1", "TP2", "TP3"]}
    return plan


class Harness(unittest.TestCase):
    def setUp(self):
        self.sup, self.broker, self.store, self.notifier = build_supervisor()
        self.broker.hedging = True
        self.broker.set_rules(replace(RULES, epic="GOLD", value_per_point=100.0,
                                      min_deal_size=0.01, size_step=0.01))
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 10_000.0}
        self.auto = self.sup.auto
        cfg = self.sup.config.auto
        cfg.enabled, cfg.instruments = True, ["GOLD"]
        self.plan = approved_plan()
        self.sup._plan_for = lambda epic, **kw: self.plan
        self.now = TUESDAY_10
        for patcher in (mock.patch.object(auto_module.AutoTrader, "_now", lambda s: self.now),
                        mock.patch("tmbot.store.utcnow", lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sup.config.management.exit_model = "three_deals"

    def turn_on(self):
        reply = self.auto.on_command("")
        self.assertTrue(self.auto.is_on(), reply)
        return reply

    def opens(self):
        return [call for call in self.broker.calls if call[0] == "open_position"]

    def closes(self):
        return [call for call in self.broker.calls if call[0] == "close_position"]

    def reasons(self):
        return [e["message"].split(":")[0] for e in self.store.recent_events(50)
                if e["kind"] == "auto_reject"]


class SwitchTests(Harness):
    def test_it_is_off_by_default_and_places_nothing(self):
        self.assertFalse(self.auto.is_on())
        self.auto._next_scan = 0.0
        self.auto.step()
        self.assertEqual(self.opens(), [])

    def test_the_config_master_switch_must_be_on(self):
        self.sup.config.auto.enabled = False
        self.assertIn("auto.enabled: false", self.auto.on_command(""))
        self.assertFalse(self.auto.is_on())

    def test_a_clear_on_message_says_what_happens_next(self):
        text = self.turn_on()
        self.assertIn("Automatic trading is ON (DEMO)", text)
        self.assertIn("Risk 1% per trade", text)
        self.assertIn("daily loss limit 3%", text)
        self.assertIn("/auto_off stops new entries", text)
        self.assertIn("/panic", text)

    def test_it_refuses_a_risk_that_is_not_1_2_or_3(self):
        self.sup.config.report.risk_percent = 1.5
        self.assertIn("not 1, 2 or 3", self.auto.on_command(""))
        self.assertFalse(self.auto.is_on())

    def test_it_refuses_when_the_terminal_blocks_orders(self):
        self.broker.algo_trading_enabled = lambda: False
        self.assertIn("Algo Trading", self.auto.on_command(""))
        self.assertFalse(self.auto.is_on())

    def test_it_refuses_a_netting_account_for_three_deals(self):
        self.broker.hedging = False
        self.assertIn("hedging", self.auto.on_command(""))
        self.assertFalse(self.auto.is_on())

    def test_it_refuses_with_no_instruments(self):
        self.sup.config.auto.instruments = []
        self.sup.config.analysis.watchlist = []
        self.assertIn("No instruments", self.auto.on_command(""))

    def test_off_stops_new_entries_at_once_and_leaves_positions_alone(self):
        self.turn_on()
        self.broker.seed_position(position())
        self.sup.tick()
        self.auto.scan("GOLD")           # would be rejected; just prove nothing closes
        reply = self.auto.off_command()
        self.assertFalse(self.auto.is_on())
        self.assertIn("NOT closed", reply)
        self.auto._next_scan = 0.0
        self.auto.step()
        self.assertEqual(self.opens(), [])
        self.assertEqual(self.closes(), [])
        self.assertEqual(len(self.broker.positions()), 1)

    def test_every_change_of_state_is_logged(self):
        self.turn_on()
        self.auto.off_command()
        kinds = [(e["kind"], e["message"]) for e in self.store.recent_events(10)]
        self.assertTrue(any(k == "auto_state" and "ON" in m for k, m in kinds))
        self.assertTrue(any(k == "auto_state" and "OFF" in m for k, m in kinds))

    def test_a_live_account_needs_both_switches_and_a_code(self):
        self.sup.config.broker.environment = "live"
        self.assertIn("auto.allow_live is false", self.auto.on_command(""))
        self.sup.config.auto.allow_live = True
        asked = self.auto.on_command("")
        self.assertIn("REAL orders", asked)
        self.assertFalse(self.auto.is_on())
        code = self.auto._codes["live_on"][0]
        self.assertIn("send /auto_on " + code, asked)
        self.assertIn("REAL orders", self.auto.on_command("0000" if code != "0000" else "1111"))
        self.assertFalse(self.auto.is_on())
        self.auto.on_command(self.auto._codes["live_on"][0])      # a wrong guess issues a new code
        self.assertTrue(self.auto.is_on())

    def test_a_live_code_works_once_and_expires(self):
        self.sup.config.broker.environment = "live"
        self.sup.config.auto.allow_live = True
        self.auto.on_command("")
        code = self.auto._codes["live_on"][0]
        self.auto._codes["live_on"] = (code, time.monotonic() - 1)
        self.auto.on_command(code)
        self.assertFalse(self.auto.is_on(), "an expired code must not work")
        self.auto.on_command("")
        code = self.auto._codes["live_on"][0]
        self.auto.on_command(code)
        self.assertTrue(self.auto.is_on())
        self.auto.off_command()
        self.auto.on_command(code)
        self.assertFalse(self.auto.is_on(), "a code must not be reusable")

    def test_a_live_account_never_resumes_on_restart(self):
        self.turn_on()
        self.sup.config.broker.environment = "live"
        self.auto.restore()
        self.assertFalse(self.auto.is_on())
        self.assertIn("never resumes", texts(self.notifier))

    def test_a_demo_account_keeps_its_state_across_a_restart(self):
        self.turn_on()
        self.auto.restore()
        self.assertTrue(self.auto.is_on())

    def test_turning_the_master_switch_off_stops_it_at_restart(self):
        self.turn_on()
        self.sup.config.auto.enabled = False
        self.auto.restore()
        self.assertFalse(self.auto.is_on())

    def test_the_old_auto_command_does_nothing(self):
        before = self.auto.is_on()
        self.assertIn("/auto_on", self.auto.legacy_command(""))
        self.assertEqual(self.auto.is_on(), before)

    def test_the_commands_are_registered(self):
        seen = {}
        self.sup.notifier.register = lambda name, handler: seen.__setitem__(name, handler)
        self.sup._register_commands()
        for name in ("auto_on", "auto_off", "auto_status", "risk", "daily_report",
                     "plan_status", "panic", "msg_auto", "auto", "plan30"):
            self.assertIn(name, seen)


class GateTests(Harness):
    def verdict(self, **tweaks):
        for key, value in tweaks.items():
            setattr(self.plan, key, value)
        return self.auto.evaluate("GOLD")

    def test_a_clean_signal_is_accepted(self):
        self.turn_on()
        verdict = self.auto.evaluate("GOLD")
        self.assertTrue(verdict.accepted, verdict.reason)

    def test_an_instrument_not_on_the_list_is_refused(self):
        self.sup.config.auto.instruments = ["SILVER"]
        self.assertEqual(self.auto.evaluate("GOLD").reason, "not_permitted")

    def test_outside_the_session_and_on_weekends(self):
        for when in (datetime(2026, 10, 27, 6, 59, tzinfo=UTC),    # before the open
                     datetime(2026, 10, 27, 20, 0, tzinfo=UTC),    # the end is exclusive
                     datetime(2026, 10, 31, 12, 0, tzinfo=UTC),    # Saturday
                     datetime(2026, 11, 1, 12, 0, tzinfo=UTC)):    # Sunday
            self.now = when
            self.assertEqual(self.auto.evaluate("GOLD").reason, "outside_session", when)
        self.now = datetime(2026, 10, 27, 7, 0, tzinfo=UTC)
        self.assertTrue(self.auto.evaluate("GOLD").accepted)

    def test_the_daily_loss_limit_blocks_new_entries(self):
        self.auto.day_state()                                  # the day starts at 10,000
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 9_699.0}
        verdict = self.auto.evaluate("GOLD")
        self.assertEqual(verdict.reason, "daily_limit")
        self.assertIn("3%", self.t(verdict))

    def test_just_inside_the_limit_still_trades(self):
        self.auto.day_state()
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 9_701.0}
        self.assertTrue(self.auto.evaluate("GOLD").accepted)

    def t(self, verdict):
        return Translator()(f"auto.reject.{verdict.reason}", **verdict.args)

    def test_the_limit_follows_the_days_start_not_an_old_high(self):
        self.auto.day_state()
        self.now += timedelta(days=1)                          # a new trading day
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 9_000.0}
        self.assertTrue(self.auto.evaluate("GOLD").accepted, "a new day starts a fresh limit")

    def test_unreadable_equity_means_no_trade(self):
        def broken():
            raise RetryableError("down")
        self.broker.account_summary = broken
        self.assertEqual(self.auto.evaluate("GOLD").reason, "equity_unknown")

    def test_an_open_position_on_the_instrument_blocks_a_second(self):
        self.broker.seed_position(position())
        self.assertEqual(self.auto.evaluate("GOLD").reason, "open_on_instrument")

    def test_the_open_deal_cap_counts_every_position_on_the_account(self):
        self.sup.config.auto.max_open_positions = 2
        self.broker.seed_position(replace(position(), epic="SILVER"))
        self.broker.seed_position(replace(position(deal_id="d2"), epic="OIL"))
        self.assertEqual(self.auto.evaluate("GOLD").reason, "positions_full")

    def test_a_basket_that_would_exceed_the_cap_is_refused(self):
        self.sup.config.auto.max_open_positions = 4
        self.broker.seed_position(replace(position(), epic="SILVER"))
        self.broker.seed_position(replace(position(deal_id="d2"), epic="OIL"))
        verdict = self.auto.evaluate("GOLD")     # 2 open + 3 legs > 4
        self.assertEqual(verdict.reason, "positions_full")

    def test_an_unsettled_order_blocks_everything(self):
        self.store.set(auto_module.PENDING_KEY, '{"epic": "GOLD", "ts": "x"}')
        self.assertEqual(self.auto.evaluate("GOLD").reason, "order_pending")

    def test_a_failed_analysis_is_not_traded(self):
        def boom(epic, **kw):
            raise RuntimeError("no candles")
        self.sup._plan_for = boom
        verdict = self.auto.evaluate("GOLD")
        self.assertEqual(verdict.reason, "no_plan")

    def test_missing_broker_data_is_not_traded(self):
        self.broker.fail_next = RetryableError("positions unavailable")
        self.assertEqual(self.auto.evaluate("GOLD").reason, "data_error")

    def test_a_neutral_or_advisory_plan_is_not_traded(self):
        self.assertEqual(self.verdict(bias=Bias.NEUTRAL).reason, "no_direction")
        self.plan.bias = Bias.BULLISH
        self.assertEqual(self.verdict(advisory_only=True).reason, "no_direction")

    def test_levels_against_the_analysis_are_not_traded(self):
        self.assertEqual(self.verdict(direction=Direction.SELL).reason, "against_bias")

    def test_only_an_approved_verdict_trades(self):
        for verdict in ("CAUTION", "REJECTED"):
            self.plan.assessment = {**self.plan.assessment, "verdict": verdict}
            self.assertEqual(self.auto.evaluate("GOLD").reason, "not_approved")

    def test_a_weak_signal_is_refused(self):
        self.assertEqual(self.verdict(confidence=59.0).reason, "weak_signal")
        self.plan.confidence = 60.0
        self.assertTrue(self.auto.evaluate("GOLD").accepted)

    def test_a_higher_timeframe_that_is_not_aligned_is_refused(self):
        for state in ("CONFLICT", "PARTIAL", "UNCERTAIN"):
            self.plan.assessment = {**self.plan.assessment, "alignment": state}
            self.assertEqual(self.auto.evaluate("GOLD").reason, "not_aligned", state)

    def test_a_likely_reversal_is_refused(self):
        flags = [{"key": "extended", "severity": "minor", "args": {"atr": "2.4"}},
                 {"key": "rsi_stretched", "severity": "minor", "args": {"rsi": "75"}},
                 {"key": "near_level", "severity": "minor", "args": {"level": 1, "atr": "0.2"}},
                 {"key": "timeframe_partial", "severity": "minor", "args": {"timeframe": "H4"}}]
        self.plan.assessment = {**self.plan.assessment, "flags": flags}
        self.assertEqual(self.auto.evaluate("GOLD").reason, "doubtful")

    def test_a_trade_that_will_probably_hit_its_stop_first_is_refused(self):
        self.plan.tp1 = 3400.0 + 0.5            # TP1 is a sixth of the stop away
        self.assertEqual(self.auto.evaluate("GOLD").reason, "doubtful")

    def test_a_risk_limit_that_cannot_be_met_is_never_forced(self):
        self.plan.sizing = {"verdict": "REJECTED", "legs": 0, "risk_percent": 1.0,
                            "max_money": 100.0, "min_lot_money": 340.0, "currency": "USD"}
        verdict = self.auto.evaluate("GOLD")
        self.assertEqual(verdict.reason, "risk_rejected")
        self.assertIn("not widened", self.t(verdict))

    def test_an_unchecked_risk_is_not_traded(self):
        self.plan.sizing = {"verdict": "UNCHECKED"}
        self.assertEqual(self.auto.evaluate("GOLD").reason, "risk_unchecked")
        self.plan.sizing = {}
        self.assertEqual(self.auto.evaluate("GOLD").reason, "risk_unchecked")

    def test_a_plan_sized_for_another_risk_percent_is_refused(self):
        self.sup.config.report.risk_percent = 2.0
        self.assertEqual(self.auto.evaluate("GOLD").reason, "risk_mismatch")

    def test_a_market_that_is_not_tradeable_is_refused(self):
        self.broker.set_rules(replace(RULES, epic="GOLD", value_per_point=100.0, tradeable=False))
        self.assertEqual(self.auto.evaluate("GOLD").reason, "not_tradeable")

    def test_the_cooldown_blocks_the_same_side_but_not_the_other(self):
        self.store.set(f"{auto_module.LAST_KEY}GOLD:BUY", (self.now - timedelta(hours=1)).isoformat())
        self.assertEqual(self.auto.evaluate("GOLD").reason, "cooldown")
        self.store.set(f"{auto_module.LAST_KEY}GOLD:BUY", (self.now - timedelta(hours=5)).isoformat())
        self.assertTrue(self.auto.evaluate("GOLD").accepted)


class OrderTests(Harness):
    def place(self):
        self.turn_on()
        return self.auto.scan("GOLD")

    def test_a_basket_opens_one_deal_per_leg_with_its_own_stop_and_target(self):
        verdict = self.place()
        self.assertTrue(verdict.accepted)
        opens = self.opens()
        self.assertEqual(len(opens), 3)
        for (_, args, kwargs), target in zip(opens, (self.plan.tp1, self.plan.tp2, self.plan.tp3)):
            self.assertEqual(args, ("GOLD", Direction.BUY, 0.11))
            self.assertEqual(kwargs["stop_level"], self.plan.sl)
            self.assertEqual(kwargs["profit_level"], target)

    def test_the_stop_is_the_plans_technical_stop_never_widened(self):
        self.place()
        stops = {kw["stop_level"] for _, _, kw in self.opens()}
        self.assertEqual(stops, {3397.0})

    def test_a_short_sends_a_sell_below_the_stop(self):
        self.plan = approved_plan(direction=Direction.SELL)
        self.place()
        self.assertTrue(all(args[1] is Direction.SELL for _, args, _ in self.opens()))
        self.assertTrue(all(kw["stop_level"] == 3403.0 for _, _, kw in self.opens()))

    def test_fewer_legs_use_tp1_and_tp3(self):
        self.plan.sizing = {**self.plan.sizing, "legs": 2, "per_leg": 0.16}
        self.place()
        self.assertEqual([kw["profit_level"] for _, _, kw in self.opens()],
                         [self.plan.tp1, self.plan.tp3])

    def test_the_opened_message_shows_the_risk_numbers(self):
        self.place()
        text = texts(self.notifier)
        self.assertIn("AUTO OPENED: GOLD", text)
        self.assertIn("LONG (BUY)", text)
        self.assertIn("3 x 0.11 lots", text)
        self.assertIn("SL 3397.0 (distance 3.00)", text)
        self.assertIn("Risk 1% = max 100.00 USD", text)
        self.assertIn("estimated loss at SL 99.00 USD", text)

    def test_every_request_and_acceptance_is_logged(self):
        self.place()
        events = self.store.recent_events(50)
        orders = [e for e in events if e["kind"] == "auto_order"]
        self.assertEqual(len(orders), 6)         # three requests, three acceptances
        self.assertTrue(any(e["kind"] == "auto_open" for e in events))

    def test_a_second_scan_cannot_repeat_the_trade(self):
        self.place()
        self.assertEqual(self.auto.scan("GOLD").reason, "open_on_instrument")
        self.assertEqual(len(self.opens()), 3)

    def test_closing_it_does_not_allow_an_instant_re_entry(self):
        self.place()
        for position_ in list(self.broker.positions()):
            self.broker.close_position(position_.deal_id)
        self.assertEqual(self.auto.scan("GOLD").reason, "cooldown")
        self.assertEqual(len(self.opens()), 3)

    def test_the_markers_are_written_before_the_order_goes_out(self):
        seen = {}
        original = self.broker.open_position

        def spy(*args, **kwargs):
            seen["pending"] = self.store.get(auto_module.PENDING_KEY)
            seen["last"] = self.store.get(f"{auto_module.LAST_KEY}GOLD:BUY")
            return original(*args, **kwargs)
        self.broker.open_position = spy
        self.place()
        self.assertTrue(seen["pending"] and seen["last"])
        self.assertEqual(self.store.get(auto_module.PENDING_KEY), "", "cleared once settled")

    def test_a_failure_part_way_stops_the_basket_and_never_retries(self):
        original = self.broker.open_position
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise PermanentError("market rejected the order", code="10016")
            return original(*args, **kwargs)
        self.broker.open_position = flaky
        verdict = self.place()
        self.assertEqual(verdict.reason, "order_failed")
        self.assertEqual(len(calls), 2, "no third deal and no retry")
        self.assertEqual(len(self.broker.positions()), 1)
        self.assertIn("1 of 3 deal(s) opened", texts(self.notifier))
        self.assertEqual(self.auto.scan("GOLD").reason, "open_on_instrument")

    def test_an_order_that_times_out_is_settled_from_the_account_not_resent(self):
        def timeout(*args, **kwargs):
            raise RetryableError("timed out")
        self.broker.open_position = timeout
        verdict = self.place()
        self.assertEqual(verdict.reason, "order_failed")
        self.assertEqual(self.broker.positions(), [])
        self.assertEqual(self.store.get(auto_module.PENDING_KEY), "")
        self.assertEqual(self.auto.scan("GOLD").reason, "cooldown", "no instant resend")
        self.assertIn("timed out", texts(self.notifier))

    def test_a_total_failure_closes_the_adoption_window(self):
        self.sup.start()

        def refuse(*args, **kwargs):
            raise PermanentError("rejected", code="10016")
        self.broker.open_position = refuse
        self.place()
        self.broker.seed_position(replace(position(), deal_id="manual-9"))
        self.sup.tick()
        self.assertEqual([t.deal_id for t in self.store.pending_trades()], ["manual-9"],
                         "a manual deal after a failed order must still ask you")

    def test_an_unreadable_account_after_an_order_keeps_the_block(self):
        original = self.broker.positions
        state = {"fail": False}

        def positions():
            if state["fail"]:
                raise RetryableError("down")
            return original()
        original_open = self.broker.open_position

        def open_then_drop(*args, **kwargs):
            reference = original_open(*args, **kwargs)
            state["fail"] = True
            return reference
        self.broker.positions = positions
        self.broker.open_position = open_then_drop
        self.place()
        self.assertTrue(self.store.get(auto_module.PENDING_KEY), "unsettled: stays blocked")
        state["fail"] = False
        self.assertEqual(self.auto.evaluate("GOLD").reason, "order_pending")
        self.auto.reconcile_pending()
        self.assertEqual(self.store.get(auto_module.PENDING_KEY), "")

    def test_a_dry_run_sends_nothing(self):
        self.sup.config.dry_run = True
        verdict = self.place()
        self.assertTrue(verdict.accepted)
        self.assertEqual(self.opens(), [])
        self.assertIn("DRY RUN", texts(self.notifier))
        self.assertEqual(self.auto.scan("GOLD").reason, "cooldown")

    def test_a_good_signal_stopped_by_a_guard_is_told_once(self):
        self.turn_on()
        self.auto.day_state()
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 9_600.0}
        self.auto.scan("GOLD")
        self.auto.scan("GOLD")
        warnings = [m for m in self.notifier.messages if "daily loss limit reached" in m[1]]
        self.assertEqual(len(warnings), 1)
        self.assertIn("open trades are still managed", warnings[0][1])
        self.assertEqual(self.reasons().count("daily_limit"), 2, "both decisions are logged")

    def test_a_routine_rejection_is_logged_but_not_messaged(self):
        self.turn_on()
        self.plan.confidence = 10.0
        self.auto.scan("GOLD")
        self.assertEqual(self.notifier.messages, [])
        self.assertIn("weak_signal", self.reasons())

    def test_a_scan_that_blows_up_is_contained_and_reported(self):
        self.turn_on()
        self.auto.scan = mock.Mock(side_effect=RuntimeError("boom"))
        self.auto._next_scan = 0.0
        self.auto.step()                             # must not raise
        self.assertIn("scan failed: boom", texts(self.notifier))


class SchedulingTests(Harness):
    def test_one_instrument_per_scan_in_turn(self):
        self.sup.config.auto.instruments = ["GOLD", "SILVER"]
        self.turn_on()
        scanned = []
        self.auto.scan = lambda epic: scanned.append(epic)
        for _ in range(3):
            self.auto._next_scan = 0.0
            self.auto.step()
        self.assertEqual(scanned, ["GOLD", "SILVER", "GOLD"])

    def test_scans_are_spaced_by_scan_minutes(self):
        self.turn_on()
        scanned = []
        self.auto.scan = lambda epic: scanned.append(epic)
        self.auto._next_scan = 0.0
        self.auto.step()
        self.auto.step()
        self.assertEqual(len(scanned), 1)

    def test_a_degraded_connection_pauses_scanning(self):
        self.turn_on()
        self.sup._degraded = True
        scanned = []
        self.auto.scan = lambda epic: scanned.append(epic)
        self.auto._next_scan = 0.0
        self.auto.step()
        self.assertEqual(scanned, [])


class AdoptionTests(Harness):
    def test_our_own_deals_are_managed_without_a_confirm(self):
        self.sup.start()
        self.turn_on()
        self.auto.scan("GOLD")
        self.sup.tick()
        trades = self.store.active_trades()
        self.assertEqual(len(trades), 3)
        self.assertTrue(all(t.status is TradeStatus.MANAGING for t in trades))
        self.assertEqual(self.store.pending_trades(), [])
        self.assertNotIn("/confirm", texts(self.notifier))

    def test_a_deal_you_open_yourself_still_waits_for_your_confirm(self):
        self.sup.start()
        self.turn_on()
        self.broker.seed_position(replace(position(), deal_id="manual-1"))
        self.sup.tick()
        self.assertEqual(len(self.store.pending_trades()), 1)

    def test_the_window_only_covers_the_side_we_opened(self):
        self.sup.start()
        self.turn_on()
        self.auto.scan("GOLD")                             # BUY basket
        self.broker.seed_position(replace(position(deal_id="manual-2",
                                                   direction=Direction.SELL), size=0.5))
        self.sup.tick()
        self.assertEqual([t.deal_id for t in self.store.pending_trades()], ["manual-2"])

    def test_managing_continues_after_the_daily_limit_and_after_off(self):
        self.sup.start()
        self.turn_on()
        self.auto.scan("GOLD")
        self.sup.tick()
        self.auto.day_state()
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 9_000.0}
        self.auto.off_command()
        self.broker.set_quote("GOLD", 3404.0, 3404.2)
        before = len(self.broker.calls)
        self.sup.tick()
        managed = self.store.active_trades()
        self.assertEqual(len(managed), 2, "leg 1 was closed at TP1; legs 2 and 3 are still managed")
        self.assertTrue(all(t.status is TradeStatus.MANAGING for t in managed))
        closed = [c for c in self.broker.calls[before:] if c[0] == "close_position"]
        self.assertEqual(len(closed), 1, "price passed TP1: management still acts after OFF and the limit")
        self.assertEqual(len(self.opens()), 3, "no new entry after OFF or the limit")


class PanicTests(Harness):
    def test_panic_needs_a_code_then_closes_managed_trades_and_turns_auto_off(self):
        self.sup.start()
        self.turn_on()
        self.auto.scan("GOLD")
        self.sup.tick()
        asked = self.auto.panic_command("")
        self.assertIn("send /panic", asked)
        self.assertEqual(self.closes(), [])
        self.assertTrue(self.auto.is_on())
        self.auto.panic_command("nope")
        self.assertEqual(self.closes(), [])
        code = self.auto._codes["panic"][0]
        text = self.auto.panic_command(code)
        self.assertFalse(self.auto.is_on())
        self.assertIn("Closing 3 managed trade(s)", text)
        self.assertEqual(len(self.closes()), 3)
        self.assertEqual(self.broker.positions(), [])


class RiskCommandTests(Harness):
    def test_one_two_and_three_are_the_only_choices(self):
        self.assertEqual(RISK_CHOICES, (1.0, 2.0, 3.0))
        for bad in ("0", "0.5", "1.5", "4", "10", "abc", "-1"):
            before = self.sup.config.report.risk_percent
            self.assertIn("1, 2 or 3", self.auto.risk_command(bad), bad)
            self.assertEqual(self.sup.config.report.risk_percent, before)

    def test_the_default_is_one_percent_and_shows_the_money(self):
        text = self.auto.risk_command("")
        self.assertIn("Risk per trade: 1% of equity = 100.00", text)
        self.assertEqual(self.sup.config.report.risk_percent, 1.0)

    def test_choosing_two_is_stored_applied_and_clearly_flagged(self):
        text = self.auto.risk_command("2")
        self.assertEqual(self.sup.config.report.risk_percent, 2.0)
        self.assertEqual(self.store.get("risk_percent"), "2")
        self.assertIn("higher-risk setting", text)
        self.assertIn("200.00", text)
        self.assertNotIn("higher-risk setting. Your choice", self.auto.risk_command("1"))

    def test_the_choice_survives_a_restart(self):
        self.auto.risk_command("3%")
        self.sup.config.report.risk_percent = 1.0
        self.auto.load_risk_override()
        self.assertEqual(self.sup.config.report.risk_percent, 3.0)

    def test_a_stored_value_that_is_not_a_choice_is_ignored(self):
        self.store.set("risk_percent", "7")
        self.auto.load_risk_override()
        self.assertEqual(self.sup.config.report.risk_percent, 1.0)

    def test_nothing_ever_raises_the_risk_by_itself(self):
        self.turn_on()
        self.plan.sizing = {"verdict": "REJECTED", "legs": 0, "risk_percent": 1.0,
                            "max_money": 100.0, "min_lot_money": 340.0, "currency": "USD"}
        self.auto.scan("GOLD")
        self.assertEqual(self.sup.config.report.risk_percent, 1.0)
        self.assertEqual(self.opens(), [])

    def test_the_plan30_rule_check_follows_the_selected_risk(self):
        from tmbot.analysis.plan30 import Plan30
        Plan30(start_date=TUESDAY_10.date()).save(self.store)
        self.auto.risk_command("2")
        self.assertEqual(self.sup.plan30._load().risk, 0.02)


class ReportTests(Harness):
    def test_status_shows_on_or_off_and_the_settings(self):
        text = self.auto.status_command()
        self.assertIn("Automatic trading: OFF (DEMO)", text)
        self.turn_on()
        text = self.auto.status_command()
        self.assertIn("Automatic trading: ON (DEMO)", text)
        self.assertIn("Risk 1% per trade", text)
        self.assertIn("Instruments: GOLD", text)
        self.assertIn("Session 07:00–20:00 UTC Mon–Fri: open now", text)
        self.assertIn("Open deals 0 of 3", text)
        self.assertIn("daily loss limit 3% (0% used)", text)

    def test_status_says_when_the_config_will_refuse(self):
        self.sup.config.auto.enabled = False
        self.assertIn("auto.enabled is false", self.auto.status_command())

    def test_status_lists_the_latest_decisions(self):
        self.turn_on()
        self.plan.confidence = 5.0
        self.auto.scan("GOLD")
        self.assertIn("Latest decisions:", self.auto.status_command())
        self.assertIn("weak_signal", self.auto.status_command())

    def test_status_flags_a_reached_limit(self):
        self.auto.day_state()
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 9_650.0}
        self.assertIn("no new entries until tomorrow", self.auto.status_command())

    def test_the_daily_report_has_equity_risk_results_and_counts(self):
        self.turn_on()
        self.auto.scan("GOLD")
        self.broker.account_summary = lambda: {"currency": "USD", "equity": 9_950.0}
        text = self.auto.daily_report_command()
        self.assertIn("Daily report — 2026-10-27", text)
        self.assertIn("Equity 10,000.00 → 9,950.00 (-50.00, -0.50%)", text)
        self.assertIn("Open: 3 of 3 deals", text)
        self.assertIn("Daily loss limit 3%: 17% used", text)
        self.assertIn("Risk 1% per trade = max 99.50", text)
        self.assertIn("orders 6, opened 1", text)

    def test_the_daily_report_without_equity_says_so(self):
        def broken():
            raise RetryableError("down")
        self.broker.account_summary = broken
        self.assertIn("could not be read", self.auto.daily_report_command())

    def test_plan_status_is_the_30_day_status(self):
        seen = {}
        self.sup.notifier.register = lambda name, handler: seen.__setitem__(name, handler)
        self.sup._register_commands()
        self.assertIn("No 30-day plan", seen["plan_status"](""))

    def test_arabic_has_every_new_string(self):
        for key, entry in CATALOG.items():
            if key.startswith(("auto.", "risk.cmd_")):
                self.assertTrue(entry.get("ar"), key)

    def test_the_arabic_replies_render_without_missing_wording(self):
        self.sup.t = Translator("ar")
        self.turn_on()
        for text in (self.auto.status_command(), self.auto.daily_report_command(),
                     self.auto.risk_command("2"), self.auto.off_command(),
                     self.auto.panic_command(""), self.auto.legacy_command("")):
            self.assertNotIn("[auto.", text)
            self.assertNotIn("[risk.", text)

    def test_every_rejection_reason_has_wording(self):
        reasons = ["not_permitted", "outside_session", "equity_unknown", "daily_limit",
                   "data_error", "open_on_instrument", "positions_full", "order_pending",
                   "no_plan", "no_direction", "against_bias", "not_approved", "weak_signal",
                   "not_aligned", "doubtful", "risk_unchecked", "risk_rejected",
                   "risk_mismatch", "not_tradeable", "cooldown", "order_failed"]
        for reason in reasons:
            self.assertIn(f"auto.reject.{reason}", CATALOG, reason)
            self.assertTrue(CATALOG[f"auto.reject.{reason}"].get("ar"), reason)


class ConfigTests(unittest.TestCase):
    def config(self, **auto):
        from tmbot.config import Config
        c = Config()
        c.broker.environment = "demo"
        c.broker.api_key = c.broker.identifier = c.broker.password = "x"
        for key, value in auto.items():
            setattr(c.auto, key, value)
        return c

    def check(self, **auto):
        self.config(**auto).validate()

    def test_defaults_are_off_and_safe(self):
        c = self.config()
        self.assertFalse(c.auto.enabled)
        self.assertFalse(c.auto.allow_live)
        self.assertEqual((c.auto.daily_loss_limit_percent, c.report.risk_percent), (3.0, 1.0))
        self.assertEqual((c.auto.session_start, c.auto.session_end), ("07:00", "20:00"))
        self.assertEqual(c.auto.weekdays, [0, 1, 2, 3, 4])
        c.validate()

    def test_bad_values_are_named(self):
        for bad in ({"session_start": "7am"}, {"session_end": "25:00"}, {"weekdays": []},
                    {"weekdays": [7]}, {"daily_loss_limit_percent": 0},
                    {"daily_loss_limit_percent": 101}, {"max_open_positions": 0},
                    {"min_signal_strength": 101}, {"scan_minutes": 0},
                    {"cooldown_hours": -1}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                self.check(**bad)

    def test_auto_needs_your_telegram_chat(self):
        with self.assertRaises(ConfigError):
            self.check(enabled=True)
        c = self.config(enabled=True)
        c.telegram.enabled, c.telegram.chat_id, c.telegram.bot_token = True, "123", "tok"
        c.validate()

    def test_negative_costs_are_refused(self):
        c = self.config()
        c.report.commission_per_lot = -1
        with self.assertRaises(ConfigError):
            c.validate()

    def test_the_example_config_loads_and_keeps_auto_off(self):
        import yaml
        from tmbot.config import Config, _coerce
        raw = yaml.safe_load(open("config.example.yaml"))
        loaded = _coerce(Config, raw)
        self.assertFalse(loaded.auto.enabled)

    def test_an_overnight_session_wraps_midnight(self):
        from tmbot.manage.auto import AutoTrader
        sup, *_ = build_supervisor()
        sup.config.auto.session_start, sup.config.auto.session_end = "22:00", "04:00"
        sup.config.auto.weekdays = [0, 1, 2, 3, 4, 5, 6]
        auto = sup.auto
        self.assertTrue(auto.in_session(datetime(2026, 10, 27, 23, 0, tzinfo=UTC)))
        self.assertTrue(auto.in_session(datetime(2026, 10, 27, 3, 59, tzinfo=UTC)))
        self.assertFalse(auto.in_session(datetime(2026, 10, 27, 12, 0, tzinfo=UTC)))


class SizingTests(unittest.TestCase):
    """The position-size maths behind the order: costs in, minimum lot never forced."""

    def assess(self, **kw):
        base = dict(equity=100.0, currency="USD", risk_percent=1.0, stop_distance=2.0,
                    rules=replace(GOLD, epic="GOLD"), legs_planned=3)
        base.update(kw)
        return risk_module.assess(**base)

    def test_the_hundred_dollar_account_cannot_take_a_normal_gold_stop(self):
        a = self.assess()                      # 1% of $100 = $1; 0.01 lot x $2 stop = $2
        self.assertEqual(a.verdict, risk_module.REJECTED)
        self.assertEqual((a.legs, a.per_leg), (0, 0.0))
        self.assertEqual(a.min_lot_money, 2.0)
        self.assertGreater(a.min_lot_money, a.max_money)

    def test_a_smaller_technical_stop_makes_a_smaller_lot_possible(self):
        a = self.assess(stop_distance=0.8)     # 0.01 lot x 0.8 x 100 = $0.80 <= $1
        self.assertEqual((a.verdict, a.legs, a.per_leg), (risk_module.REDUCED_LEGS, 1, 0.01))
        self.assertLessEqual(a.total_money, a.max_money)

    def test_larger_equity_reduces_the_basket_before_it_rejects(self):
        a = self.assess(equity=10_000.0, stop_distance=3.0)
        self.assertEqual(a.verdict, risk_module.OK)
        self.assertLessEqual(a.total_money, a.max_money)
        tight = self.assess(equity=300.0, stop_distance=1.2)
        self.assertEqual(tight.verdict, risk_module.REDUCED_LEGS)
        self.assertLess(tight.legs, 3)
        self.assertLessEqual(tight.total_money, tight.max_money)

    def test_two_and_three_percent_allow_more_but_never_more_than_chosen(self):
        for percent in (1.0, 2.0, 3.0):
            a = self.assess(equity=1000.0, risk_percent=percent, stop_distance=1.5)
            self.assertLessEqual(a.total_money, a.max_money + 1e-9)
            self.assertAlmostEqual(a.max_money, 1000.0 * percent / 100.0)
        low = self.assess(equity=1000.0, risk_percent=1.0, stop_distance=1.5)
        high = self.assess(equity=1000.0, risk_percent=3.0, stop_distance=1.5)
        self.assertGreater(high.selected_volume, low.selected_volume)

    def test_the_stop_is_never_widened_to_fit_a_lot_size(self):
        a = self.assess(stop_distance=2.0)
        self.assertEqual(a.stop_distance, 2.0)

    def test_the_risk_percent_is_never_raised_to_pass(self):
        a = self.assess()
        self.assertEqual(a.risk_percent, 1.0)
        self.assertEqual(a.verdict, risk_module.REJECTED)

    def test_spread_and_slippage_widen_the_loss_so_lots_shrink(self):
        plain = self.assess(equity=10_000.0, stop_distance=3.0)
        costly = self.assess(equity=10_000.0, stop_distance=3.0, spread=1.0, slippage=0.5)
        self.assertGreater(costly.stop_distance, plain.stop_distance)
        self.assertEqual(costly.stop_distance, 3.0 + 0.5 + 0.5)
        self.assertLess(costly.selected_volume, plain.selected_volume)

    def test_commission_is_in_the_loss_per_lot(self):
        plain = self.assess(equity=10_000.0, stop_distance=3.0)
        paid = self.assess(equity=10_000.0, stop_distance=3.0, commission_per_lot=7.0)
        self.assertAlmostEqual(paid.loss_per_lot, plain.loss_per_lot + 7.0, places=3)
        self.assertLess(paid.selected_volume, plain.selected_volume)
        self.assertLessEqual(paid.total_money, paid.max_money + 1e-9)

    def test_commission_can_turn_an_ok_trade_into_a_rejection(self):
        ok = self.assess(equity=100.0, stop_distance=0.5, legs_planned=1)
        self.assertEqual(ok.verdict, risk_module.OK)
        bad = self.assess(equity=100.0, stop_distance=0.5, legs_planned=1,
                          commission_per_lot=100.0)
        self.assertEqual(bad.verdict, risk_module.REJECTED)

    def test_size_is_a_multiple_of_the_broker_step_and_not_above_its_maximum(self):
        a = self.assess(equity=100_000.0, stop_distance=1.0,
                        rules=replace(GOLD, epic="GOLD", max_deal_size=0.5))
        self.assertLessEqual(a.per_leg, 0.5)
        self.assertAlmostEqual(a.per_leg / 0.01, round(a.per_leg / 0.01), places=6)

    def test_missing_inputs_mean_unchecked_not_safe(self):
        self.assertIsNone(self.assess(equity=0.0))
        self.assertIsNone(self.assess(rules=replace(GOLD, epic="GOLD", value_per_point=None)))
        self.assertIsNone(self.assess(stop_distance=0.0))

    def test_the_sizing_in_a_plan_reads_the_cost_settings(self):
        from tmbot.analysis import style as style_module
        broker = broker_with()
        config = auto_config()
        config.report.commission_per_lot = 7.0
        config.report.slippage = 0.5
        without = auto_config()
        rules = broker.market_rules("GOLD")
        a = style_module.size_for_plan(broker, config, rules, 3.0)
        b = style_module.size_for_plan(broker, without, rules, 3.0)
        self.assertGreater(a["loss_per_lot"], b["loss_per_lot"])
        self.assertEqual((a["commission_per_lot"], a["slippage"]), (7.0, 0.5))


class ShortMessageTests(unittest.TestCase):
    def test_the_short_message_shows_risk_money_lots_and_stop_distance(self):
        from tmbot.analysis.report import render_text
        plan = approved_plan()
        text = render_text(plan)
        self.assertIn("SL 3397.0 (distance 3.00)", text)
        self.assertIn("Risk 1% (max loss 100.00 USD)", text)
        self.assertIn("est. loss at SL 99.00 USD", text)
        self.assertIn("Decision: Buy 3 x 0.11 lots", text)


if __name__ == "__main__":
    unittest.main()
