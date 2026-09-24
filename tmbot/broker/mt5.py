"""MetaTrader 5 adapter (Exness or any other MT5 broker).

The ``MetaTrader5`` Python package does not talk to the broker itself: it
drives a MetaTrader 5 terminal running on the same Windows PC, and the terminal
holds the connection to the broker. So:

* Windows only -- the package ships no build for anything else.
* The terminal must be installed, and its "Algo Trading" button must be on, or
  every stop move and close is refused with retcode 10027.
* A dropped connection is the terminal's to recover; this adapter re-attaches
  to the terminal if the link between Python and the terminal breaks.

Positions are identified by their MT5 ticket, used here as the deal id.

This adapter never opens a position. Partial closes are done natively, by
sending an opposite deal that names the position's ticket, so no opening
order is ever needed.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..config import BrokerConfig
from ..errors import ConfigError, NotSupportedError, PermanentError, RetryableError
from ..models import BrokerPosition, Candle, Direction, MarketRules, Quote, utcnow
from ..util.retry import CircuitBreaker, RetryPolicy, call_with_retry
from .base import BrokerAdapter, PartialCloseProbe, PartialCloseStrategy

log = logging.getLogger(__name__)

# Values from the MetaTrader5 package, repeated so this module imports (and
# can be tested) on machines where the package cannot be installed.
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
ORDER_TIME_GTC = 0
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_CLOSEONLY = 3
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_OUT_BY = 3
# symbol_info().filling_mode is a bitmask of these
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

TIMEFRAMES: Dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 1 | 0x4000,
    "H4": 4 | 0x4000,
    "D1": 24 | 0x4000,
    "W1": 1 | 0x8000,
}

# order_send() retcodes.
DONE = {10008, 10009, 10010}   # placed, done, done partially
NO_CHANGES = 10025             # the stop is already where we asked
RETRYABLE_RETCODES = {
    10004: "requote",
    10011: "request processing error",
    10012: "request timed out",
    10018: "market is closed",
    10020: "prices changed",
    10021: "no quotes to process the request",
    10024: "too many requests",
    10028: "request locked for processing",
    10029: "position frozen: price is too close to the stop or target",
    10031: "no connection to the trade server",
}
PERMANENT_RETCODES = {
    10006: "request rejected by the broker",
    10013: "invalid request",
    10014: "invalid volume",
    10015: "invalid price",
    10016: "invalid stop or target: too close to the current price",
    10017: "trading is disabled for this account",
    10019: "not enough money",
    10026: "the broker has disabled algorithmic trading on this account",
    10027: (
        "Algo Trading is switched OFF in MetaTrader 5 -- click the Algo "
        "Trading button in the terminal's toolbar so it turns green"
    ),
    10030: "this fill type is not supported for the symbol",
    10036: "the position is already closed",
    10038: "close volume is larger than the position",
    10044: "only closing is allowed for this symbol",
    10045: "the account only allows closing positions in FIFO order",
}
# Filling types to fall back through if the broker refuses one with 10030.
INVALID_FILL = 10030
PRICE_RETRY = {10004, 10020, 10021}

# last_error() codes from the package.
RES_E_AUTH_FAILED = -6
RES_E_AUTO_TRADING_DISABLED = -8
IPC_ERRORS = range(-10005, -9999)   # -10000 .. -10005: lost the terminal

# People search for "gold", MT5 names it XAUUSD.
SEARCH_ALIASES = {
    "gold": "xau",
    "silver": "xag",
    "bitcoin": "btc",
    "ethereum": "eth",
    "oil": "oil",
    "brent": "brent",
    "gas": "ng",
    "natural gas": "ng",
}


def explain_retcode(retcode: int) -> str:
    """Plain-language meaning of an order_send() retcode."""
    return (
        RETRYABLE_RETCODES.get(retcode)
        or PERMANENT_RETCODES.get(retcode)
        or ""
    )


def _field(record: Any, name: str, default: Any = None) -> Any:
    """Read a field from an MT5 named tuple, numpy row or plain dict."""
    if isinstance(record, dict):
        return record.get(name, default)
    try:
        return record[name]
    except (TypeError, KeyError, IndexError, ValueError):
        return getattr(record, name, default)


def _decimals(step: float) -> int:
    text = f"{step:.10f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


class MT5Broker(BrokerAdapter):
    name = "mt5"

    def __init__(self, config: BrokerConfig, module: Any = None):
        self.config = config
        self._module = module
        # The MetaTrader5 package holds one connection per process and is not
        # safe to call from two threads at once. Telegram commands run on their
        # own thread, so every call goes through this lock.
        self._lock = threading.RLock()
        self._attached = False
        self._account: Any = None
        self._rules_cache: Dict[str, MarketRules] = {}
        # "xauusdm" typed by hand -> "XAUUSDm", the name MT5 actually uses.
        self._names: Dict[str, str] = {}
        self._policy = RetryPolicy(
            attempts=config.retry_attempts,
            base_delay=config.retry_base_delay,
            max_delay=config.retry_max_delay,
        )
        self.breaker = CircuitBreaker(
            threshold=config.circuit_threshold,
            cooldown=config.circuit_cooldown,
            name="mt5",
        )
        self.partial_close_strategy = PartialCloseStrategy.DELETE_WITH_SIZE

    # ------------------------------------------------------------------ plumbing

    @property
    def mt5(self) -> Any:
        if self._module is None:
            try:
                import MetaTrader5  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ConfigError(
                    "the MetaTrader5 package is not installed. It only exists "
                    "for Windows: run  pip install MetaTrader5  inside the "
                    "bot's .venv on your Windows PC"
                ) from exc
            self._module = MetaTrader5
        return self._module

    @property
    def _offset(self) -> timedelta:
        return timedelta(hours=self.config.mt5.server_utc_offset_hours)

    def _server_time(self, seconds: Any) -> Optional[datetime]:
        """Convert an MT5 timestamp (broker server time) to real UTC."""
        if not seconds:
            return None
        stamp = datetime.fromtimestamp(float(seconds), tz=timezone.utc)
        return stamp - self._offset

    def _last_error(self) -> tuple[int, str]:
        try:
            code, message = self.mt5.last_error()
            return int(code), str(message)
        except Exception:  # pragma: no cover - defensive
            return -1, "unknown error"

    def _raise_last_error(self, what: str) -> None:
        code, message = self._last_error()
        text = f"{what} failed: MT5 error {code} {message}".strip()
        if code in IPC_ERRORS:
            # The Python side lost the terminal (closed, crashed, restarting).
            self._attached = False
            raise RetryableError(
                f"{text} -- the MetaTrader 5 terminal is not reachable; "
                "is it open and logged in?"
            )
        if code == RES_E_AUTH_FAILED:
            raise PermanentError(
                f"{text} -- MetaTrader 5 refused the login. Check the account "
                "number, the TRADING password and the server name in .env",
                code=str(code),
            )
        if code == RES_E_AUTO_TRADING_DISABLED:
            raise PermanentError(f"{text} -- {PERMANENT_RETCODES[10027]}", code=str(code))
        if code == -2:  # invalid parameters: a bug here, not a hiccup
            raise PermanentError(text, code=str(code))
        raise RetryableError(text)

    def _call(self, what: str, fn_name: str, *args: Any, allow_empty: bool = False, **kwargs: Any) -> Any:
        """Call one MetaTrader5 function with retry and circuit breaking.

        The package signals failure by returning None and leaving the reason
        in last_error().
        """

        def attempt() -> Any:
            with self._lock:
                if not self._attached:
                    self._attach()
                result = getattr(self.mt5, fn_name)(*args, **kwargs)
                if result is None:
                    code, _ = self._last_error()
                    if allow_empty and code in (0, 1, -4):
                        return None
                    self._raise_last_error(what)
                return result

        return call_with_retry(
            lambda: self.breaker.call(attempt), self._policy, description=what
        )

    # ------------------------------------------------------------------ session

    def _attach(self) -> None:
        """Start (or reconnect to) the terminal and log in.  Caller holds the lock."""
        login = self.config.active_mt5
        if not login.configured:
            environment = self.config.environment.upper()
            raise ConfigError(
                f"no MetaTrader 5 login for {self.config.environment}: set "
                f"MT5_{environment}_LOGIN, MT5_{environment}_PASSWORD and "
                f"MT5_{environment}_SERVER in .env"
            )
        kwargs: Dict[str, Any] = {
            "login": int(str(login.login).strip()),
            "password": login.password,
            "server": login.server,
            "timeout": int(self.config.mt5.connect_timeout_ms),
        }
        if login.terminal_path:
            ok = self.mt5.initialize(login.terminal_path, **kwargs)
        else:
            ok = self.mt5.initialize(**kwargs)
        if not ok:
            code, message = self._last_error()
            try:
                self.mt5.shutdown()
            except Exception:  # pragma: no cover - best effort
                pass
            if code == RES_E_AUTH_FAILED:
                raise PermanentError(
                    f"MetaTrader 5 refused the login ({code} {message}). Check "
                    "the account number, the TRADING password and the exact "
                    "server name -- all three are in the email the broker sent "
                    "when the account was opened",
                    code=str(code),
                )
            raise RetryableError(
                f"could not start or reach MetaTrader 5 ({code} {message}). "
                "Is the terminal installed? If you have more than one, set "
                "MT5_*_TERMINAL to the full path of terminal64.exe"
            )

        account = self.mt5.account_info()
        if account is None:
            self._raise_last_error("account_info")
        if int(_field(account, "login", 0)) != kwargs["login"]:
            raise PermanentError(
                f"MetaTrader 5 is logged in to account {_field(account, 'login')}, "
                f"not {kwargs['login']} -- log in to the right account in the terminal"
            )
        self._check_environment(account)
        self._account = account
        self._attached = True
        log.info(
            "attached to MetaTrader 5: account %s on %s",
            _field(account, "login"), _field(account, "server"),
        )

    def _check_environment(self, account: Any) -> None:
        """Refuse to run --env demo on a real account, or --env live on a demo one.

        The environment flag decides which database the trades are journaled
        in and which safety gates apply, so it must say what the account
        really is.
        """
        mode = int(_field(account, "trade_mode", -1))
        is_real = mode == ACCOUNT_TRADE_MODE_REAL
        wanted_real = self.config.environment == "live"
        if mode < 0 or is_real == wanted_real:
            return
        kind = "REAL-money" if is_real else "DEMO"
        raise PermanentError(
            f"you chose --env {self.config.environment} but MetaTrader 5 account "
            f"{_field(account, 'login')} is a {kind} account. Put the right "
            f"account in MT5_{self.config.environment.upper()}_* in .env"
        )

    def connect(self) -> None:
        with self._lock:
            self._attach()
        if self.algo_trading_enabled() is False:
            log.warning(
                "Algo Trading is OFF in MetaTrader 5: stops cannot be moved or "
                "deals closed until it is switched on"
            )

    def close(self) -> None:
        with self._lock:
            if self._attached:
                try:
                    self.mt5.shutdown()
                except Exception:  # pragma: no cover - best effort
                    log.debug("mt5 shutdown failed", exc_info=True)
            self._attached = False

    # ------------------------------------------------------------------ account

    def account_info(self) -> Any:
        account = self._call("account_info", "account_info")
        self._account = account
        return account

    def terminal_info(self) -> Any:
        return self._call("terminal_info", "terminal_info")

    def account_summary(self) -> Dict[str, Any]:
        account = self.account_info()
        mode = int(_field(account, "trade_mode", -1))
        return {
            "accountId": str(_field(account, "login", "")),
            "accountName": f"{_field(account, 'name', '')} ({_field(account, 'server', '')})",
            "currency": _field(account, "currency", ""),
            "balance": _field(account, "balance"),
            "equity": _field(account, "equity"),
            "leverage": _field(account, "leverage"),
            "company": _field(account, "company", ""),
            "server": _field(account, "server", ""),
            "accountType": "real" if mode == ACCOUNT_TRADE_MODE_REAL else "demo",
        }

    def hedging_mode(self) -> Optional[bool]:
        try:
            account = self.account_info()
        except (PermanentError, RetryableError) as exc:
            log.warning("could not read the account: %s", exc)
            return None
        return int(_field(account, "margin_mode", -1)) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING

    def set_hedging_mode(self, enabled: bool) -> Dict[str, Any]:
        raise NotSupportedError(
            "MetaTrader 5 fixes hedging or netting when the account is opened; "
            "it cannot be switched. Open a new account of the hedging type if "
            "this one nets"
        )

    def algo_trading_enabled(self) -> Optional[bool]:
        try:
            terminal = self.terminal_info()
            account = self.account_info()
        except (PermanentError, RetryableError):
            return None
        return bool(_field(terminal, "trade_allowed", True)) and bool(
            _field(account, "trade_expert", True)
        )

    # ------------------------------------------------------------------ market data

    def _name(self, symbol: str) -> str:
        return self._names.get(symbol, symbol)

    def _symbol_info(self, symbol: str) -> Any:
        symbol = self._name(symbol)
        info = self._call(f"symbol_info {symbol}", "symbol_info", symbol, allow_empty=True)
        if info is not None and _field(info, "visible", True):
            return info
        # A symbol hidden from Market Watch has no live prices until selected.
        with self._lock:
            selected = self.mt5.symbol_select(symbol, True)
        if not selected:
            # MT5 names are case-sensitive; forgive the capitalisation.
            for other in self._call("list symbols", "symbols_get") or ():
                real = str(_field(other, "name", ""))
                if real.upper() == symbol.upper() and real != symbol:
                    self._names[symbol] = real
                    return self._symbol_info(real)
            raise PermanentError(
                f"MetaTrader 5 has no symbol called {symbol!r} on this account. "
                "Symbol names differ between brokers and account types (Exness "
                "adds a letter such as 'm' for some accounts) -- run the "
                "'markets' command to find the exact name",
                code="unknown-symbol",
            )
        return self._call(f"symbol_info {symbol}", "symbol_info", symbol)

    def market_rules(self, epic: str) -> MarketRules:
        info = self._symbol_info(epic)
        epic = self._name(epic)
        point = float(_field(info, "point", 0.0) or 0.0)
        levels = max(
            int(_field(info, "trade_stops_level", 0) or 0),
            int(_field(info, "trade_freeze_level", 0) or 0),
        )
        mode = int(_field(info, "trade_mode", 4))
        step = float(_field(info, "volume_step", 0.0) or 0.0)
        rules = MarketRules(
            epic=epic,
            name=str(_field(info, "description", "") or epic),
            min_deal_size=float(_field(info, "volume_min", 0.0) or 0.0),
            size_step=step,
            decimal_places=int(_field(info, "digits", 2)),
            min_stop_distance=levels * point,
            min_stop_distance_is_pct=False,
            tradeable=mode != SYMBOL_TRADE_MODE_DISABLED,
        )
        self._rules_cache[epic] = rules
        return rules

    def quote(self, epic: str) -> Quote:
        epic = self._name(epic)
        tick = self._call(f"quote {epic}", "symbol_info_tick", epic, allow_empty=True)
        if tick is None:
            self._symbol_info(epic)  # selects it, or explains that it does not exist
            epic = self._name(epic)
            tick = self._call(f"quote {epic}", "symbol_info_tick", epic)
        bid, ask = float(_field(tick, "bid", 0.0)), float(_field(tick, "ask", 0.0))
        if bid <= 0 or ask <= 0:
            raise RetryableError(f"no bid/ask for {epic} yet")
        return Quote(epic=epic, bid=bid, ask=ask, ts=utcnow())

    def candles(self, epic: str, timeframe: str, limit: int) -> List[Candle]:
        key = timeframe.upper()
        if key not in TIMEFRAMES:
            raise ConfigError(f"unknown timeframe {timeframe!r}; expected one of {sorted(TIMEFRAMES)}")
        self._symbol_info(epic)
        epic = self._name(epic)
        # The first request for a symbol can come back empty while the terminal
        # downloads its history, which the retry covers.
        rates = self._call(
            f"prices {epic} {key}", "copy_rates_from_pos",
            epic, TIMEFRAMES[key], 0, max(1, int(limit)),
        )
        candles = []
        for rate in rates:
            stamp = self._server_time(_field(rate, "time"))
            if stamp is None:
                continue
            candles.append(Candle(
                ts=stamp,
                open=float(_field(rate, "open")),
                high=float(_field(rate, "high")),
                low=float(_field(rate, "low")),
                close=float(_field(rate, "close")),
                volume=float(_field(rate, "tick_volume", 0) or 0),
            ))
        candles.sort(key=lambda candle: candle.ts)
        return candles[-limit:]

    def search_markets(self, term: str) -> List[Dict[str, Any]]:
        symbols = self._call("list symbols", "symbols_get") or ()
        wanted = term.strip().lower()
        needles = {wanted, SEARCH_ALIASES.get(wanted, wanted)}
        found = []
        for info in symbols:
            name = str(_field(info, "name", ""))
            haystack = " ".join(
                str(_field(info, part, "") or "") for part in ("name", "description", "path")
            ).lower()
            if not any(needle and needle in haystack for needle in needles):
                continue
            mode = int(_field(info, "trade_mode", 4))
            status = {
                SYMBOL_TRADE_MODE_DISABLED: "DISABLED",
                SYMBOL_TRADE_MODE_CLOSEONLY: "CLOSE_ONLY",
            }.get(mode, "TRADEABLE")
            found.append({
                "epic": name,
                "instrumentName": str(_field(info, "description", "") or name),
                "marketStatus": status,
            })
        found.sort(key=lambda market: (len(market["epic"]), market["epic"]))
        return found

    # ------------------------------------------------------------------ positions

    def _currency(self) -> str:
        return str(_field(self._account, "currency", "") or "") if self._account else ""

    def _to_position(self, raw: Any) -> BrokerPosition:
        created = self._server_time(
            (_field(raw, "time_msc", 0) or 0) / 1000.0 or _field(raw, "time", 0)
        )
        stop = float(_field(raw, "sl", 0.0) or 0.0)
        target = float(_field(raw, "tp", 0.0) or 0.0)
        return BrokerPosition(
            deal_id=str(_field(raw, "ticket")),
            epic=str(_field(raw, "symbol")),
            direction=Direction.BUY if int(_field(raw, "type")) == ORDER_TYPE_BUY else Direction.SELL,
            size=float(_field(raw, "volume")),
            entry_price=float(_field(raw, "price_open")),
            stop_level=stop or None,       # MT5 uses 0.0 for "no stop"
            profit_level=target or None,
            currency=self._currency(),
            created_at=created,
            upl=float(_field(raw, "profit", 0.0) or 0.0),
        )

    def positions(self) -> List[BrokerPosition]:
        raw = self._call("positions", "positions_get", allow_empty=True) or ()
        return [self._to_position(item) for item in raw]

    def _raw_position(self, deal_id: str) -> Any:
        try:
            ticket = int(deal_id)
        except (TypeError, ValueError):
            return None
        raw = self._call(f"position {deal_id}", "positions_get", ticket=ticket, allow_empty=True)
        return raw[0] if raw else None

    def position(self, deal_id: str) -> Optional[BrokerPosition]:
        raw = self._raw_position(deal_id)
        return self._to_position(raw) if raw is not None else None

    # ------------------------------------------------------------------ orders

    def _send(self, request: Dict[str, Any], what: str) -> Any:
        """Send one order and turn its retcode into success or a typed error."""
        with self._lock:
            if not self._attached:
                self._attach()
            result = self.mt5.order_send(request)
        if result is None:
            # Whether it reached the server is unknown; the next cycle re-reads
            # the position before deciding anything again.
            self._raise_last_error(what)
        retcode = int(_field(result, "retcode", 0))
        if retcode in DONE or retcode == NO_CHANGES:
            return result
        comment = _field(result, "comment", "") or ""
        meaning = explain_retcode(retcode)
        message = f"{what} -> retcode {retcode} {comment}".strip()
        if meaning:
            message = f"{message} ({meaning})"
        error: Exception = (
            RetryableError(message) if retcode in RETRYABLE_RETCODES
            else PermanentError(message, code=str(retcode))
        )
        error.retcode = retcode  # type: ignore[attr-defined]
        raise error

    def modify_position(
        self,
        deal_id: str,
        *,
        stop_level: Optional[float] = None,
        profit_level: Optional[float] = None,
    ) -> str:
        if stop_level is None and profit_level is None:
            raise ValueError("modify_position needs a stop_level or a profit_level")
        raw = self._raw_position(deal_id)
        if raw is None:
            raise PermanentError(f"modify {deal_id}: position not found", code="not-found")
        # MT5 sets stop AND target together; leaving one out would erase it.
        request = {
            "action": TRADE_ACTION_SLTP,
            "position": int(deal_id),
            "symbol": _field(raw, "symbol"),
            "sl": float(stop_level if stop_level is not None else (_field(raw, "sl") or 0.0)),
            "tp": float(profit_level if profit_level is not None else (_field(raw, "tp") or 0.0)),
        }
        result = self._send(request, f"modify {deal_id}")
        return str(_field(result, "request_id", "") or deal_id)

    def _filling_modes(self, symbol: str) -> List[int]:
        """Fill types to try, most likely to be accepted first."""
        info = self._symbol_info(symbol)
        allowed = int(_field(info, "filling_mode", 0) or 0)
        modes = []
        if allowed & SYMBOL_FILLING_FOK:
            modes.append(ORDER_FILLING_FOK)
        if allowed & SYMBOL_FILLING_IOC:
            modes.append(ORDER_FILLING_IOC)
        modes.append(ORDER_FILLING_RETURN)
        for mode in (ORDER_FILLING_FOK, ORDER_FILLING_IOC):
            if mode not in modes:
                modes.append(mode)
        return modes

    def close_position(self, deal_id: str, size: Optional[float] = None) -> str:
        raw = self._raw_position(deal_id)
        if raw is None:
            raise PermanentError(f"close {deal_id}: position not found", code="not-found")
        symbol = str(_field(raw, "symbol"))
        held = float(_field(raw, "volume"))
        volume = held if size is None else min(float(size), held)
        rules = self._rules_cache.get(symbol) or self.market_rules(symbol)
        if size is not None and rules.size_step:
            volume = round(
                rules.round_size(volume), max(_decimals(rules.size_step), 2)
            )
        if volume <= 0:
            raise PermanentError(f"close {deal_id}: nothing to close (size {size})")
        closing_buy = int(_field(raw, "type")) == ORDER_TYPE_BUY

        refused: Optional[Exception] = None
        for filling in self._filling_modes(symbol):
            for _ in range(3):
                tick = self.quote(symbol)
                request = {
                    "action": TRADE_ACTION_DEAL,
                    "position": int(deal_id),
                    "symbol": symbol,
                    "volume": volume,
                    "type": ORDER_TYPE_SELL if closing_buy else ORDER_TYPE_BUY,
                    "price": tick.bid if closing_buy else tick.ask,
                    "deviation": int(self.config.mt5.deviation_points),
                    "type_time": ORDER_TIME_GTC,
                    "type_filling": filling,
                    "comment": "tmbot exit",
                }
                try:
                    result = self._send(request, f"close {volume} of {deal_id}")
                except (PermanentError, RetryableError) as exc:
                    retcode = getattr(exc, "retcode", None)
                    if retcode in PRICE_RETRY:
                        refused = exc
                        continue  # the price moved: requote and go again
                    if retcode == INVALID_FILL:
                        refused = exc
                        break  # this fill type is refused: try the next
                    raise
                return str(_field(result, "deal", "") or _field(result, "order", "") or deal_id)
            else:
                # Requoted every time: leave it for the next cycle.
                assert refused is not None
                raise refused
        assert refused is not None
        raise refused

    def open_position(
        self,
        epic: str,
        direction: Direction,
        size: float,
        *,
        stop_level: Optional[float] = None,
        profit_level: Optional[float] = None,
    ) -> str:
        raise NotSupportedError(
            "the bot never opens positions; on MetaTrader 5 partial closes go "
            "through the position's own ticket instead"
        )

    def confirm(self, deal_reference: str) -> Dict[str, Any]:
        # order_send() is synchronous: its retcode already was the confirmation.
        return {"dealReference": deal_reference, "dealStatus": "ACCEPTED"}

    def closing_price(self, deal_id: str) -> Optional[float]:
        try:
            ticket = int(deal_id)
        except (TypeError, ValueError):
            return None
        deals = self._call(
            f"history {deal_id}", "history_deals_get", position=ticket, allow_empty=True
        ) or ()
        exits = [
            deal for deal in deals
            if int(_field(deal, "entry", -1)) in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)
        ]
        if not exits:
            return None
        # Volume-weighted over every exit, in case it was closed in pieces.
        volume = sum(float(_field(deal, "volume", 0.0)) for deal in exits)
        if volume <= 0:
            return float(_field(exits[-1], "price"))
        return sum(
            float(_field(deal, "price")) * float(_field(deal, "volume")) for deal in exits
        ) / volume

    # ------------------------------------------------------------------ capability probe

    def probe_partial_close(self, *, needed: bool = True) -> PartialCloseProbe:
        """MT5 closes part of a position by ticket natively -- nothing to test.

        No order is sent. The only thing worth reporting is the account's
        hedging/netting type, which the three-deal model depends on.
        """
        hedging = self.hedging_mode()
        notes = [
            "account type = " + (
                "hedging" if hedging else "netting" if hedging is False else "unknown"
            ),
            "MetaTrader 5 closes part of a position by its ticket natively, so "
            "no opening order is ever needed",
        ]
        if self.config.partial_close_strategy.lower() == "netting_offset":
            notes.append(
                "broker.partial_close_strategy netting_offset ignored: that path "
                "opens an opposite deal, which this adapter never does"
            )
        self.partial_close_strategy = PartialCloseStrategy.DELETE_WITH_SIZE
        return PartialCloseProbe(
            strategy=PartialCloseStrategy.DELETE_WITH_SIZE,
            hedging_mode=hedging,
            delete_accepts_size=True,
            notes=notes,
            needed=needed,
        )
