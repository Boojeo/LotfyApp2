"""MetaTrader 5 adapter, against a fake of the MetaTrader5 package.

The real package only exists for Windows and needs a running terminal, so
these tests drive the adapter through a stand-in that answers the way the
package does: named records, None plus last_error() on failure, retcodes on
order_send().
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from tmbot.broker.base import PartialCloseStrategy
from tmbot.broker.factory import build_broker
from tmbot.broker.mt5 import MT5Broker
from tmbot.config import BrokerConfig, Config, ConfigError, MT5Account, _coerce, apply_env
from tmbot.errors import NotSupportedError, PermanentError, RetryableError
from tmbot.manage.engine import TradeEngine
from tmbot.manage.rules import evaluate
from tmbot.models import Direction, Stage, TradeStatus
from tmbot.notify.base import NullNotifier
from tmbot.store import Store
from tests.helpers import management, snapshot, trade

OPENED = int(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc).timestamp())


class FakeMT5:
    """Just enough of the MetaTrader5 package to exercise the adapter."""

    def __init__(self, *, trade_mode=0, margin_mode=2, filling_mode=1):
        self.error = (1, "Success")
        self.initialized = False
        self.init_calls = []
        self.requests = []
        self.replies = []          # queued retcodes for order_send
        self.fail_next = {}        # function name -> (code, message)
        self.trade_allowed = True
        self.account = SimpleNamespace(
            login=5550001, server="Exness-MT5Trial8", name="Test", company="Exness",
            currency="USD", balance=10000.0, equity=10000.0, leverage=200,
            trade_mode=trade_mode, margin_mode=margin_mode, trade_expert=True,
        )
        self.symbols = {
            "XAUUSDm": SimpleNamespace(
                name="XAUUSDm", description="Gold vs US Dollar", path="Metals\\XAUUSDm",
                visible=True, point=0.001, digits=3, volume_min=0.01, volume_step=0.01,
                trade_stops_level=0, trade_freeze_level=0, trade_mode=4,
                filling_mode=filling_mode,
            ),
            "USOILm": SimpleNamespace(
                name="USOILm", description="Crude Oil WTI", path="Energies\\USOILm",
                visible=False, point=0.001, digits=3, volume_min=0.01, volume_step=0.01,
                trade_stops_level=10, trade_freeze_level=0, trade_mode=4,
                filling_mode=filling_mode,
            ),
        }
        self.ticks = {"XAUUSDm": SimpleNamespace(bid=3410.0, ask=3410.2)}
        self.open = {}
        self.history = []
        self._next_deal = 900

    # -- plumbing
    def _fail(self, name):
        if name in self.fail_next:
            self.error = self.fail_next.pop(name)
            return True
        self.error = (1, "Success")
        return False

    def initialize(self, *args, **kwargs):
        self.init_calls.append((args, kwargs))
        if self._fail("initialize"):
            return False
        self.initialized = True
        return True

    def shutdown(self):
        self.initialized = False

    def last_error(self):
        return self.error

    # -- account
    def account_info(self):
        return None if self._fail("account_info") else self.account

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=self.trade_allowed, connected=True)

    # -- symbols
    def symbol_info(self, symbol):
        return self.symbols.get(symbol)

    def symbol_select(self, symbol, enable):
        if symbol in self.symbols:
            self.symbols[symbol].visible = True
            self.ticks.setdefault(symbol, SimpleNamespace(bid=65.0, ask=65.03))
            return True
        return False

    def symbol_info_tick(self, symbol):
        info = self.symbols.get(symbol)
        return self.ticks.get(symbol) if info is not None and info.visible else None

    def symbols_get(self, group=None):
        return tuple(self.symbols.values())

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        if self._fail("copy_rates_from_pos"):
            return None
        # Newest last, broker server time, like the real thing.
        return [
            {"time": OPENED + 900 * index, "open": 3400.0 + index, "high": 3402.0 + index,
             "low": 3399.0 + index, "close": 3401.0 + index, "tick_volume": 10}
            for index in range(count)
        ]

    # -- positions
    def seed(self, ticket, *, type_=0, volume=0.03, price=3400.0, sl=3390.0, tp=0.0,
             symbol="XAUUSDm"):
        self.open[ticket] = SimpleNamespace(
            ticket=ticket, symbol=symbol, type=type_, volume=volume, price_open=price,
            sl=sl, tp=tp, profit=0.0, time=OPENED, time_msc=OPENED * 1000,
        )

    def positions_get(self, **kwargs):
        if self._fail("positions_get"):
            return None
        if "ticket" in kwargs:
            found = self.open.get(kwargs["ticket"])
            return (found,) if found else ()
        return tuple(self.open.values())

    def order_send(self, request):
        self.requests.append(dict(request))
        if self.replies:
            code = self.replies.pop(0)
            if code not in (10009, 10008, 10010, 10025):
                return SimpleNamespace(retcode=code, comment="refused", order=0, deal=0,
                                       request_id=1)
        held = self.open.get(request.get("position"))
        if request["action"] == 6:
            held.sl, held.tp = request["sl"], request["tp"]
            return SimpleNamespace(retcode=10009, comment="done", order=0, deal=0,
                                   request_id=7)
        if held is None:
            return SimpleNamespace(retcode=10036, comment="closed", order=0, deal=0,
                                   request_id=1)
        self._next_deal += 1
        self.history.append(SimpleNamespace(
            position_id=held.ticket, entry=1, volume=request["volume"],
            price=request["price"], ticket=self._next_deal,
        ))
        held.volume = round(held.volume - request["volume"], 8)
        if held.volume <= 0:
            del self.open[held.ticket]
        return SimpleNamespace(retcode=10009, comment="done", order=self._next_deal,
                               deal=self._next_deal, request_id=8)

    def history_deals_get(self, position=None, **kwargs):
        return tuple(deal for deal in self.history if deal.position_id == position)


def settings(environment="demo", **mt5_overrides) -> BrokerConfig:
    config = BrokerConfig(environment=environment, platform="mt5")
    config.retry_attempts = 2
    config.retry_base_delay = 0.0
    config.retry_max_delay = 0.0
    account = MT5Account(login="5550001", password="pw", server="Exness-MT5Trial8")
    config.mt5.demo = account
    config.mt5.live = account
    for key, value in mt5_overrides.items():
        setattr(config.mt5, key, value)
    return config


def connected(fake=None, **kwargs):
    fake = fake or FakeMT5()
    broker = MT5Broker(settings(**kwargs), module=fake)
    broker.connect()
    return broker, fake


class ConnectionTests(unittest.TestCase):
    def test_logs_in_with_the_account_number_password_and_server(self):
        broker, fake = connected()
        _, kwargs = fake.init_calls[0]
        self.assertEqual(kwargs["login"], 5550001)
        self.assertEqual(kwargs["server"], "Exness-MT5Trial8")
        summary = broker.account_summary()
        self.assertEqual(summary["accountId"], "5550001")
        self.assertEqual(summary["accountType"], "demo")

    def test_a_specific_terminal_is_started_when_a_path_is_given(self):
        config = settings()
        config.mt5.demo = MT5Account(login="5550001", password="pw",
                                     server="Exness-MT5Trial8",
                                     terminal_path=r"C:\Exness\terminal64.exe")
        fake = FakeMT5()
        MT5Broker(config, module=fake).connect()
        args, _ = fake.init_calls[0]
        self.assertEqual(args, (r"C:\Exness\terminal64.exe",))

    def test_live_flag_on_a_demo_account_is_refused(self):
        broker = MT5Broker(settings("live"), module=FakeMT5(trade_mode=0))
        with self.assertRaises(PermanentError) as caught:
            broker.connect()
        self.assertIn("DEMO account", str(caught.exception))

    def test_demo_flag_on_a_real_account_is_refused(self):
        broker = MT5Broker(settings("demo"), module=FakeMT5(trade_mode=2))
        with self.assertRaises(PermanentError) as caught:
            broker.connect()
        self.assertIn("REAL-money", str(caught.exception))

    def test_a_wrong_password_is_reported_as_such_and_not_retried(self):
        fake = FakeMT5()
        fake.fail_next["initialize"] = (-6, "Terminal: Authorization failed")
        with self.assertRaises(PermanentError) as caught:
            MT5Broker(settings(), module=fake).connect()
        self.assertIn("TRADING password", str(caught.exception))
        self.assertEqual(len(fake.init_calls), 1)

    def test_a_lost_terminal_is_reattached_and_the_call_replayed(self):
        broker, fake = connected()
        fake.seed(101)
        fake.fail_next["positions_get"] = (-10004, "No IPC connection")
        self.assertEqual([p.deal_id for p in broker.positions()], ["101"])
        self.assertEqual(len(fake.init_calls), 2, "re-initialised after the drop")

    def test_hedging_comes_from_the_account_margin_mode(self):
        self.assertTrue(connected()[0].hedging_mode())
        self.assertFalse(connected(FakeMT5(margin_mode=0))[0].hedging_mode())

    def test_hedging_cannot_be_switched(self):
        with self.assertRaises(NotSupportedError):
            connected()[0].set_hedging_mode(True)

    def test_algo_trading_button_is_reported(self):
        broker, fake = connected()
        self.assertTrue(broker.algo_trading_enabled())
        fake.trade_allowed = False
        self.assertFalse(broker.algo_trading_enabled())


class MarketDataTests(unittest.TestCase):
    def test_rules_come_from_symbol_info(self):
        rules = connected()[0].market_rules("XAUUSDm")
        self.assertEqual(rules.min_deal_size, 0.01)
        self.assertEqual(rules.size_step, 0.01)
        self.assertEqual(rules.decimal_places, 3)
        self.assertEqual(rules.name, "Gold vs US Dollar")

    def test_a_hidden_symbol_is_selected_before_use(self):
        broker, fake = connected()
        rules = broker.market_rules("USOILm")
        self.assertTrue(fake.symbols["USOILm"].visible)
        self.assertAlmostEqual(rules.min_stop_distance, 0.01)
        self.assertEqual(broker.quote("USOILm").bid, 65.0)

    def test_an_unknown_symbol_points_at_the_markets_command(self):
        with self.assertRaises(PermanentError) as caught:
            connected()[0].market_rules("GOLD")
        self.assertIn("markets", str(caught.exception))

    def test_capitalisation_typed_by_hand_is_forgiven(self):
        broker, _ = connected()
        self.assertEqual(broker.market_rules("XAUUSDM").epic, "XAUUSDm")
        self.assertEqual(broker.quote("xauusdm").epic, "XAUUSDm")
        self.assertEqual(len(broker.candles("XAUUSDM", "H1", 2)), 2)

    def test_typed_names_take_the_watchlist_spelling(self):
        from tmbot.config import EpicConfig
        config = Config()
        config.analysis.watchlist = [EpicConfig(epic="XAUUSDm")]
        self.assertEqual(config.canonical_epic("gold"), "GOLD", "Capital.com unchanged")
        config.broker.platform = "mt5"
        self.assertEqual(config.canonical_epic(" xauusdm "), "XAUUSDm")
        self.assertEqual(config.canonical_epic("USOILm"), "USOILm")

    def test_candles_are_converted_from_server_time(self):
        broker, _ = connected(server_utc_offset_hours=3)
        bars = broker.candles("XAUUSDm", "M15", 5)
        self.assertEqual(len(bars), 5)
        first = datetime.fromtimestamp(OPENED, tz=timezone.utc)
        self.assertEqual(bars[0].ts.hour, first.hour - 3)
        self.assertEqual(bars[-1].close, 3405.0)

    def test_an_empty_first_history_download_is_retried(self):
        broker, fake = connected()
        fake.fail_next["copy_rates_from_pos"] = (-1, "Terminal: Call failed")
        self.assertEqual(len(broker.candles("XAUUSDm", "H1", 3)), 3)

    def test_search_finds_gold_by_its_everyday_name(self):
        found = connected()[0].search_markets("gold")
        self.assertEqual(found[0]["epic"], "XAUUSDm")
        self.assertEqual(connected()[0].search_markets("oil")[0]["epic"], "USOILm")


class PositionTests(unittest.TestCase):
    def test_positions_map_to_the_bot_model(self):
        broker, fake = connected()
        fake.seed(101, type_=1, sl=0.0, tp=3350.0)
        held = broker.position("101")
        self.assertEqual(held.deal_id, "101")
        self.assertIs(held.direction, Direction.SELL)
        self.assertIsNone(held.stop_level, "MT5's 0.0 means no stop")
        self.assertEqual(held.profit_level, 3350.0)
        self.assertEqual(held.currency, "USD")
        self.assertEqual(held.created_at, datetime.fromtimestamp(OPENED, tz=timezone.utc))
        self.assertIsNone(broker.position("999"))

    def test_moving_the_stop_keeps_the_existing_target(self):
        broker, fake = connected()
        fake.seed(101, sl=3390.0, tp=3450.0)
        broker.modify_position("101", stop_level=3400.0)
        sent = fake.requests[-1]
        self.assertEqual((sent["action"], sent["position"]), (6, 101))
        self.assertEqual((sent["sl"], sent["tp"]), (3400.0, 3450.0))

    def test_partial_close_names_the_ticket_and_sells_at_the_bid(self):
        broker, fake = connected()
        fake.seed(101, volume=0.04)
        broker.close_position("101", size=0.02)
        sent = fake.requests[-1]
        self.assertEqual((sent["action"], sent["position"], sent["type"]), (1, 101, 1))
        self.assertEqual((sent["volume"], sent["price"]), (0.02, 3410.0))
        self.assertAlmostEqual(fake.open[101].volume, 0.02)

    def test_closing_a_short_buys_at_the_ask(self):
        broker, fake = connected()
        fake.seed(102, type_=1)
        broker.close_position("102")
        self.assertEqual((fake.requests[-1]["type"], fake.requests[-1]["price"]), (0, 3410.2))
        self.assertNotIn(102, fake.open)

    def test_a_close_never_asks_for_more_than_is_held(self):
        broker, fake = connected()
        fake.seed(101, volume=0.03)
        broker.close_position("101", size=0.5)
        self.assertEqual(fake.requests[-1]["volume"], 0.03)

    def test_a_refused_fill_type_falls_back_to_the_next(self):
        broker, fake = connected(FakeMT5(filling_mode=3))
        fake.seed(101)
        fake.replies = [10030]
        broker.close_position("101")
        self.assertEqual([r["type_filling"] for r in fake.requests], [0, 1])
        self.assertNotIn(101, fake.open)

    def test_a_requote_is_retried_with_a_fresh_price(self):
        broker, fake = connected()
        fake.seed(101)
        fake.replies = [10004]
        broker.close_position("101")
        self.assertEqual(len(fake.requests), 2)

    def test_algo_trading_off_is_a_clear_permanent_error(self):
        broker, fake = connected()
        fake.seed(101)
        fake.replies = [10027]
        with self.assertRaises(PermanentError) as caught:
            broker.modify_position("101", stop_level=3400.0)
        self.assertIn("Algo Trading", str(caught.exception))

    def test_a_closed_market_is_retried_later_not_failed(self):
        broker, fake = connected()
        fake.seed(101)
        fake.replies = [10018]
        with self.assertRaises(RetryableError):
            broker.modify_position("101", stop_level=3400.0)

    def test_the_bot_cannot_open_a_position(self):
        with self.assertRaises(NotSupportedError):
            connected()[0].open_position("XAUUSDm", Direction.BUY, 0.01)

    def test_the_real_closing_price_is_read_from_history(self):
        broker, fake = connected()
        fake.seed(101, volume=0.02)
        broker.close_position("101", size=0.01)
        fake.ticks["XAUUSDm"] = SimpleNamespace(bid=3420.0, ask=3420.2)
        broker.close_position("101")
        self.assertAlmostEqual(broker.closing_price("101"), 3415.0)
        self.assertIsNone(broker.closing_price("555"))

    def test_partial_closes_need_no_probe_order(self):
        broker, fake = connected()
        probe = broker.probe_partial_close(needed=True)
        self.assertIs(probe.strategy, PartialCloseStrategy.DELETE_WITH_SIZE)
        self.assertFalse(probe.blocking)
        self.assertEqual(fake.requests, [], "probing must not send any order")


class EngineOnMT5Tests(unittest.TestCase):
    """The unchanged trade engine, driving the MT5 adapter."""

    def build(self):
        broker, fake = connected()
        broker.probe_partial_close()
        store = Store(":memory:")
        config = Config()
        config.management = management(exit_model="three_deals")
        engine = TradeEngine(broker, store, config, NullNotifier())
        return broker, fake, engine

    def test_first_leg_closes_whole_at_tp1_and_others_go_to_break_even(self):
        broker, fake, engine = self.build()
        fake.seed(101, volume=0.01, price=3400.0, sl=3390.0)
        fake.seed(102, volume=0.01, price=3400.0, sl=3390.0)
        first = trade(deal_id="101", epic="XAUUSDm", size=0.01,
                      leg_index=0, leg_target=Stage.TP1)
        second = trade(deal_id="102", epic="XAUUSDm", size=0.01,
                       leg_index=1, leg_target=Stage.TP2)
        rules = broker.market_rules("XAUUSDm")

        engine.apply(first, evaluate(first, snapshot(3410.0, rules=rules),
                                     engine.config.management))
        engine.apply(second, evaluate(second, snapshot(3410.0, rules=rules),
                                      engine.config.management))

        self.assertNotIn(101, fake.open, "leg 1 closed whole at TP1")
        self.assertIs(first.status, TradeStatus.CLOSED)
        self.assertEqual(fake.open[102].sl, 3400.0, "leg 2 is now risk-free")
        self.assertEqual(fake.open[102].volume, 0.01, "leg 2 untouched in size")


class ConfigTests(unittest.TestCase):
    def test_platform_defaults_to_capital(self):
        self.assertEqual(Config().broker.platform, "capital")
        self.assertEqual(type(build_broker(Config().broker)).__name__, "CapitalComBroker")

    def test_mt5_platform_builds_the_mt5_adapter(self):
        self.assertIsInstance(build_broker(settings()), MT5Broker)

    def test_yaml_mt5_section_is_read(self):
        config = _coerce(Config, {"broker": {
            "platform": "mt5",
            "mt5": {"server_utc_offset_hours": 2,
                    "demo": {"login": 5550001, "server": "Exness-MT5Trial8"}},
        }})
        self.assertEqual(config.broker.mt5.server_utc_offset_hours, 2)
        self.assertEqual(config.broker.mt5.demo.login, "5550001")

    def test_mt5_logins_come_from_env(self):
        env = {"MT5_DEMO_LOGIN": " 5550001 ", "MT5_DEMO_PASSWORD": "pw",
               "MT5_DEMO_SERVER": "Exness-MT5Trial8",
               "MT5_LIVE_LOGIN": "7770001", "MT5_LIVE_PASSWORD": "pw2",
               "MT5_LIVE_SERVER": "Exness-MT5Real8"}
        with mock.patch.dict(os.environ, env):
            config = apply_env(Config())
        config.broker.platform = "mt5"
        config.broker.environment = "demo"
        self.assertEqual(config.broker.active_mt5.login, "5550001")
        config.broker.environment = "live"
        self.assertEqual(config.broker.active_mt5.server, "Exness-MT5Real8")

    def test_validation_asks_for_mt5_names_not_capital_ones(self):
        config = Config()
        config.broker.platform = "mt5"
        config.broker.environment = "demo"
        with self.assertRaises(ConfigError) as caught:
            config.validate()
        self.assertIn("MT5_DEMO_LOGIN", str(caught.exception))
        self.assertNotIn("CAPITAL", str(caught.exception))

    def test_a_login_that_is_not_a_number_is_caught_early(self):
        config = Config()
        config.broker = settings()
        config.broker.mt5.demo = MT5Account(login="me@mail.com", password="p", server="s")
        with self.assertRaises(ConfigError):
            config.validate()

    def test_missing_package_explains_windows_only(self):
        broker = MT5Broker(settings())
        with mock.patch.dict("sys.modules", {"MetaTrader5": None}):
            with self.assertRaises(ConfigError) as caught:
                broker.mt5
        self.assertIn("Windows", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
