"""Capital.com adapter: session handling, error mapping and the capability probe."""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional

import requests

from tmbot.broker.base import PartialCloseStrategy
from tmbot.broker.capital import CapitalComBroker
from tmbot.config import BrokerConfig
from tmbot.errors import AuthError, PermanentError, RateLimitError, RetryableError
from tmbot.models import Candle, Direction, MarketRules


class FakeResponse:
    def __init__(self, status: int, body: Any = None, headers: Optional[Dict] = None):
        self.status_code = status
        self._body = {} if body is None else body
        self.headers = headers or {}
        self.text = json.dumps(self._body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class FakeSession:
    """Scripted transport: each entry is a response or an exception to raise."""

    def __init__(self, script: Dict[str, List[Any]]):
        self.script = script
        self.calls: List[tuple] = []

    def _next(self, key: str):
        queue = self.script.get(key)
        if not queue:
            raise AssertionError(f"no scripted response for {key}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        return self._next(f"POST {_path(url)}")

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        self.calls.append((method, url, json or params))
        return self._next(f"{method} {_path(url)}")

    def close(self):
        pass


def _path(url: str) -> str:
    return url.split(".com", 1)[-1] if ".com" in url else url


def session_ok():
    return FakeResponse(200, {}, {"CST": "cst-1", "X-SECURITY-TOKEN": "tok-1"})


def build(script: Dict[str, List[Any]], **overrides) -> tuple[CapitalComBroker, FakeSession]:
    config = BrokerConfig(
        environment="demo", api_key="k", identifier="u", password="p",
        retry_attempts=3, retry_base_delay=0.0, retry_max_delay=0.0,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    script.setdefault("POST /api/v1/session", [session_ok()])
    transport = FakeSession(script)
    broker = CapitalComBroker(config, session=transport)
    return broker, transport


class SessionTests(unittest.TestCase):
    def test_connect_stores_the_session_headers(self):
        broker, _ = build({})
        broker.connect()
        headers = broker._headers()
        self.assertEqual(headers["CST"], "cst-1")
        self.assertEqual(headers["X-SECURITY-TOKEN"], "tok-1")

    def test_a_401_triggers_one_re_authentication_and_a_replay(self):
        broker, transport = build({
            "POST /api/v1/session": [session_ok(), session_ok()],
            "GET /api/v1/positions": [
                FakeResponse(401, {"errorCode": "error.invalid.session.token"}),
                FakeResponse(200, {"positions": []}),
            ],
        })
        broker.connect()

        self.assertEqual(broker.positions(), [])
        logins = sum(1 for method, url, _ in transport.calls
                     if method == "POST" and url.endswith("/session"))
        self.assertEqual(logins, 2, "exactly one re-authentication")

    def test_a_401_that_survives_re_authentication_is_raised(self):
        broker, _ = build({
            "POST /api/v1/session": [session_ok()],
            "GET /api/v1/positions": [FakeResponse(401, {"errorCode": "error.invalid.session"})],
        })
        broker.connect()
        with self.assertRaises(AuthError):
            broker.positions()

    def test_an_expired_token_does_not_trip_the_circuit_breaker(self):
        broker, _ = build({
            "POST /api/v1/session": [session_ok()],
            "GET /api/v1/positions": [FakeResponse(401, {"errorCode": "error.invalid.session"})],
        }, circuit_threshold=2)
        broker.connect()
        for _ in range(3):
            with self.assertRaises(AuthError):
                broker.positions()
        self.assertFalse(broker.breaker.is_open)


class ErrorMappingTests(unittest.TestCase):
    def test_a_dropped_connection_is_retried_and_then_succeeds(self):
        broker, _ = build({
            "GET /api/v1/positions": [
                requests.ConnectionError("connection reset by peer"),
                FakeResponse(200, {"positions": []}),
            ],
        })
        broker.connect()
        self.assertEqual(broker.positions(), [])

    def test_a_timeout_is_retryable(self):
        broker, _ = build({
            "GET /api/v1/positions": [requests.Timeout("read timed out")],
        })
        broker.connect()
        with self.assertRaises(RetryableError):
            broker.positions()

    def test_a_429_carries_the_servers_retry_after(self):
        broker, _ = build({
            "GET /api/v1/positions": [
                FakeResponse(429, {"errorCode": "error.too-many.requests"}, {"Retry-After": "0"}),
                FakeResponse(200, {"positions": []}),
            ],
        })
        broker.connect()
        self.assertEqual(broker.positions(), [])

    def test_a_500_is_retried(self):
        broker, _ = build({
            "GET /api/v1/positions": [
                FakeResponse(503, {"errorCode": "error.service"}),
                FakeResponse(200, {"positions": []}),
            ],
        })
        broker.connect()
        self.assertEqual(broker.positions(), [])

    def test_a_400_is_permanent_and_not_retried(self):
        broker, transport = build({
            "PUT /api/v1/positions/d1": [
                FakeResponse(400, {"errorCode": "error.invalid.stoploss.maxvalue"}),
            ],
        })
        broker.connect()
        with self.assertRaises(PermanentError) as caught:
            broker.modify_position("d1", stop_level=1.0)
        self.assertEqual(caught.exception.status, 400)
        modifies = sum(1 for method, *_ in transport.calls if method == "PUT")
        self.assertEqual(modifies, 1, "a rejected modify must not be hammered")

    def test_repeated_failures_open_the_circuit(self):
        broker, _ = build(
            {"GET /api/v1/positions": [FakeResponse(503, {"errorCode": "error.service"})]},
            circuit_threshold=2, retry_attempts=2,
        )
        broker.connect()
        with self.assertRaises(RetryableError):
            broker.positions()
        self.assertTrue(broker.breaker.is_open)


class PositionTests(unittest.TestCase):
    def test_positions_are_parsed_from_the_wire_format(self):
        broker, _ = build({
            "GET /api/v1/positions": [FakeResponse(200, {"positions": [{
                "position": {
                    "dealId": "d1", "direction": "BUY", "size": 1.5, "level": 3400.25,
                    "stopLevel": 3390.0, "profitLevel": 3430.0, "currency": "USD",
                    "createdDateUTC": "2026-09-18T08:30:00",
                },
                "market": {"epic": "GOLD"},
            }]})],
        })
        broker.connect()
        [position] = broker.positions()
        self.assertEqual(position.deal_id, "d1")
        self.assertIs(position.direction, Direction.BUY)
        self.assertEqual(position.entry_price, 3400.25)
        self.assertEqual(position.stop_level, 3390.0)

    def test_a_missing_position_reads_as_none(self):
        broker, _ = build({
            "GET /api/v1/positions/gone": [
                FakeResponse(404, {"errorCode": "error.position.notfound"}),
            ],
        })
        broker.connect()
        self.assertIsNone(broker.position("gone"))

    def test_a_quote_comes_from_the_market_snapshot(self):
        broker, _ = build({
            "GET /api/v1/markets/GOLD": [FakeResponse(200, {
                "instrument": {"epic": "GOLD", "name": "Gold"},
                "snapshot": {"bid": 3399.8, "offer": 3400.2, "marketStatus": "TRADEABLE",
                             "decimalPlacesFactor": 2},
                "dealingRules": {
                    "minDealSize": {"unit": "POINTS", "value": 0.1},
                    "minSizeIncrement": {"unit": "POINTS", "value": 0.1},
                    "minStopOrProfitDistance": {"unit": "POINTS", "value": 0.5},
                },
            })],
        })
        broker.connect()
        quote = broker.quote("GOLD")
        self.assertEqual(quote.bid, 3399.8)
        self.assertEqual(quote.exit_price(Direction.BUY), 3399.8)
        self.assertEqual(quote.exit_price(Direction.SELL), 3400.2)
        # The rules cache is populated from the same payload.
        self.assertEqual(broker.market_rules("GOLD").min_deal_size, 0.1)


class HedgingTests(unittest.TestCase):
    def test_hedging_mode_can_be_turned_on(self):
        broker, transport = build({
            "PUT /api/v1/accounts/preferences": [FakeResponse(200, {"status": "SUCCESS"})],
        })
        broker.connect()
        broker.set_hedging_mode(True)
        sent = [body for method, url, body in transport.calls if method == "PUT"]
        self.assertEqual(sent, [{"hedgingMode": True}])

    def test_turning_hedging_off_is_sent_as_false_not_dropped(self):
        # False is falsy; a naive "skip empty fields" would silently drop it.
        broker, transport = build({
            "PUT /api/v1/accounts/preferences": [FakeResponse(200, {"status": "SUCCESS"})],
        })
        broker.connect()
        broker.set_hedging_mode(False)
        sent = [body for method, url, body in transport.calls if method == "PUT"]
        self.assertEqual(sent, [{"hedgingMode": False}])


class MarketSearchTests(unittest.TestCase):
    def test_searching_returns_the_epics_a_user_can_trade(self):
        broker, _ = build({
            "GET /api/v1/markets": [FakeResponse(200, {"markets": [
                {"epic": "GOLD", "instrumentName": "Gold", "marketStatus": "TRADEABLE"},
                {"epic": "SILVER", "instrumentName": "Silver", "marketStatus": "TRADEABLE"},
            ]})],
        })
        broker.connect()
        found = broker.search_markets("old")
        self.assertEqual([market["epic"] for market in found], ["GOLD", "SILVER"])


class ProbeTests(unittest.TestCase):
    def _probe(self, hedging: Any, delete_error: str) -> Any:
        preferences = (
            FakeResponse(200, {"hedgingMode": hedging}) if hedging is not None
            else FakeResponse(500, {"errorCode": "error.unavailable"})
        )
        broker, _ = build({
            "GET /api/v1/accounts/preferences": [preferences],
            "DELETE /api/v1/positions/tmbot-capability-probe": [
                FakeResponse(400, {"errorCode": delete_error}),
            ],
        }, retry_attempts=1)
        broker.connect()
        return broker.probe_partial_close()

    def test_a_netting_account_uses_the_offset_deal(self):
        probe = self._probe(False, "error.position.notfound")
        self.assertIs(probe.strategy, PartialCloseStrategy.NETTING_OFFSET)
        self.assertIs(probe.hedging_mode, False)

    def test_a_hedging_account_falls_back_to_delete_with_size(self):
        probe = self._probe(True, "error.position.notfound")
        self.assertIs(probe.strategy, PartialCloseStrategy.DELETE_WITH_SIZE)
        self.assertIs(probe.delete_accepts_size, True)

    def test_no_safe_path_is_reported_rather_than_guessed(self):
        probe = self._probe(True, "error.invalid.size")
        self.assertIs(probe.strategy, PartialCloseStrategy.UNSUPPORTED)
        self.assertIn("no safe partial-close path", probe.render())


class ParsingTests(unittest.TestCase):
    def test_candles_use_the_mid_of_bid_and_ask(self):
        candle = Candle.from_capital({
            "snapshotTimeUTC": "2026-09-18T08:00:00",
            "openPrice": {"bid": 100.0, "ask": 100.4},
            "highPrice": {"bid": 101.0, "ask": 101.4},
            "lowPrice": {"bid": 99.0, "ask": 99.4},
            "closePrice": {"bid": 100.5, "ask": 100.9},
            "lastTradedVolume": 1234,
        })
        self.assertEqual(candle.open, 100.2)
        self.assertEqual(candle.close, 100.7)
        self.assertEqual(candle.volume, 1234.0)

    def test_percentage_stop_distances_scale_with_price(self):
        rules = MarketRules.from_capital({
            "instrument": {"epic": "US100", "name": "US Tech 100"},
            "snapshot": {"marketStatus": "TRADEABLE", "decimalPlacesFactor": 1},
            "dealingRules": {
                "minDealSize": {"unit": "POINTS", "value": 0.1},
                "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.5},
            },
        })
        self.assertTrue(rules.min_stop_distance_is_pct)
        self.assertAlmostEqual(rules.stop_buffer(20000.0), 100.0)

    def test_sizes_round_down_to_the_increment(self):
        rules = MarketRules(epic="GOLD", min_deal_size=0.1, size_step=0.1)
        self.assertEqual(rules.round_size(0.25), 0.2)
        self.assertEqual(rules.round_size(0.99), 0.9)
        self.assertEqual(rules.round_size(0.05), 0.0)


if __name__ == "__main__":
    unittest.main()
