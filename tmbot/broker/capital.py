"""Capital.com REST adapter.

Session model: ``POST /api/v1/session`` returns ``CST`` and ``X-SECURITY-TOKEN``
headers that must accompany every later call.  They idle out (around ten
minutes), so we ping to keep them warm and transparently re-authenticate on a
401 rather than letting a management cycle fail.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from ..config import BrokerConfig
from ..errors import (
    AuthError,
    NotSupportedError,
    PermanentError,
    RateLimitError,
    RetryableError,
)
from ..models import BrokerPosition, Candle, Direction, MarketRules, Quote, utcnow
from ..util.retry import CircuitBreaker, RetryPolicy, call_with_retry
from ..util.timeframes import resolution
from .base import BrokerAdapter, PartialCloseProbe, PartialCloseStrategy

log = logging.getLogger(__name__)

# A deal id that cannot exist, used to probe request validation without
# touching a real position.
_PROBE_DEAL_ID = "tmbot-capability-probe"


class CapitalComBroker(BrokerAdapter):
    name = "capital.com"

    def __init__(self, config: BrokerConfig, session: Optional[requests.Session] = None):
        self.config = config
        self._http = session or requests.Session()
        self._cst: Optional[str] = None
        self._security_token: Optional[str] = None
        self._last_call = 0.0
        self._lock = threading.RLock()
        self._rules_cache: Dict[str, tuple[float, MarketRules]] = {}
        self._policy = RetryPolicy(
            attempts=config.retry_attempts,
            base_delay=config.retry_base_delay,
            max_delay=config.retry_max_delay,
        )
        self.breaker = CircuitBreaker(
            threshold=config.circuit_threshold,
            cooldown=config.circuit_cooldown,
            name="capital.com",
        )
        self.partial_close_strategy = PartialCloseStrategy.UNSUPPORTED

    # ------------------------------------------------------------------ session

    def connect(self) -> None:
        self._authenticate()
        if self.config.account_id:
            self._request("PUT", "/api/v1/session", json={"accountId": self.config.account_id})
        log.info("connected to %s (%s)", self.config.base_url, self.config.environment)

    def close(self) -> None:
        try:
            if self._cst:
                self._request("DELETE", "/api/v1/session", authenticated=True)
        except Exception:  # pragma: no cover - best effort logout
            log.debug("logout failed", exc_info=True)
        finally:
            self._http.close()

    def _authenticate(self) -> None:
        url = f"{self.config.base_url}/api/v1/session"
        payload = {
            "identifier": self.config.identifier,
            "password": self.config.password,
            "encryptedPassword": False,
        }
        headers = {
            "X-CAP-API-KEY": self.config.api_key,
            "Content-Type": "application/json",
        }

        def attempt() -> requests.Response:
            try:
                return self._http.post(
                    url, json=payload, headers=headers, timeout=self.config.timeout
                )
            except requests.RequestException as exc:
                raise RetryableError(f"session request failed: {exc}") from exc

        response = call_with_retry(attempt, self._policy, description="authenticate")
        if response.status_code >= 400:
            raise self._to_error(response, "authenticate")
        with self._lock:
            self._cst = response.headers.get("CST")
            self._security_token = response.headers.get("X-SECURITY-TOKEN")
            self._last_call = time.monotonic()
        if not (self._cst and self._security_token):
            raise PermanentError("session response did not carry CST/X-SECURITY-TOKEN headers")
        log.debug("authenticated; session tokens refreshed")

    def ensure_session(self) -> None:
        """Keep the session warm; re-authenticate if it has gone cold."""
        with self._lock:
            idle = time.monotonic() - self._last_call
            has_tokens = bool(self._cst and self._security_token)
        if not has_tokens:
            self._authenticate()
            return
        if idle > self.config.keepalive_seconds:
            try:
                self._request("GET", "/api/v1/ping")
            except (AuthError, PermanentError):
                self._authenticate()

    # ------------------------------------------------------------------ transport

    def _headers(self) -> Dict[str, str]:
        return {
            "X-CAP-API-KEY": self.config.api_key,
            "CST": self._cst or "",
            "X-SECURITY-TOKEN": self._security_token or "",
            "Content-Type": "application/json",
        }

    def _to_error(self, response: requests.Response, description: str) -> Exception:
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get("errorCode") or response.text[:200]
        status = response.status_code
        message = f"{description} -> HTTP {status} {code}"
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            return RateLimitError(message, float(retry_after) if retry_after else None)
        if status in (401, 403):
            return AuthError(message)
        if status >= 500:
            return RetryableError(message)
        return PermanentError(message, code=str(code), status=status)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
        description: Optional[str] = None,
        allow_reauth: bool = True,
    ) -> Dict[str, Any]:
        """Perform one API call with retry, re-auth and circuit breaking."""
        url = f"{self.config.base_url}{path}"
        label = description or f"{method} {path}"
        payload = {k: v for k, v in (json or {}).items() if v is not None} if json else None

        def attempt() -> Dict[str, Any]:
            headers = self._headers() if authenticated else {"X-CAP-API-KEY": self.config.api_key}
            try:
                response = self._http.request(
                    method, url, json=payload, params=params,
                    headers=headers, timeout=self.config.timeout,
                )
            except requests.Timeout as exc:
                raise RetryableError(f"{label} timed out: {exc}") from exc
            except requests.RequestException as exc:
                # Connection reset / DNS / TLS -- exactly the "API drop" case.
                raise RetryableError(f"{label} connection error: {exc}") from exc
            self._last_call = time.monotonic()
            if response.status_code >= 400:
                raise self._to_error(response, label)
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}

        def guarded() -> Dict[str, Any]:
            return self.breaker.call(attempt)

        try:
            return call_with_retry(guarded, self._policy, description=label)
        except AuthError:
            # Not retried by the policy above -- one re-auth, one replay.
            if not allow_reauth:
                raise
            log.warning("%s: session rejected, re-authenticating", label)
            self._authenticate()
            return self._request(
                method, path, json=json, params=params,
                authenticated=authenticated, description=label, allow_reauth=False,
            )

    # ------------------------------------------------------------------ account

    def account_summary(self) -> Dict[str, Any]:
        self.ensure_session()
        data = self._request("GET", "/api/v1/accounts")
        accounts = data.get("accounts", [])
        preferred = self.config.account_id
        for account in accounts:
            if preferred and account.get("accountId") == preferred:
                return account
            if not preferred and account.get("preferred", False):
                return account
        return accounts[0] if accounts else {}

    def hedging_mode(self) -> Optional[bool]:
        try:
            data = self._request("GET", "/api/v1/accounts/preferences")
        except (PermanentError, RetryableError) as exc:
            log.warning("could not read account preferences: %s", exc)
            return None
        value = data.get("hedgingMode")
        return bool(value) if value is not None else None

    # ------------------------------------------------------------------ market data

    def market_rules(self, epic: str, *, max_age: float = 3600.0) -> MarketRules:
        cached = self._rules_cache.get(epic)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        self.ensure_session()
        data = self._request("GET", f"/api/v1/markets/{epic}")
        rules = MarketRules.from_capital(data)
        self._rules_cache[epic] = (time.monotonic(), rules)
        return rules

    def quote(self, epic: str) -> Quote:
        self.ensure_session()
        data = self._request("GET", f"/api/v1/markets/{epic}")
        snapshot = data.get("snapshot", {})
        bid, offer = snapshot.get("bid"), snapshot.get("offer")
        if bid is None or offer is None:
            raise RetryableError(f"no bid/offer in snapshot for {epic}")
        # Refresh the rules cache for free while we have the payload.
        self._rules_cache[epic] = (time.monotonic(), MarketRules.from_capital(data))
        return Quote(epic=epic, bid=float(bid), ask=float(offer), ts=utcnow())

    def candles(self, epic: str, timeframe: str, limit: int) -> List[Candle]:
        self.ensure_session()
        # The API caps a single /prices page; walk backwards if more is asked for.
        remaining = max(1, int(limit))
        collected: List[Candle] = []
        to_param: Optional[str] = None
        while remaining > 0:
            page = min(remaining, 1000)
            params: Dict[str, Any] = {"resolution": resolution(timeframe), "max": page}
            if to_param:
                params["to"] = to_param
            data = self._request(
                "GET", f"/api/v1/prices/{epic}", params=params,
                description=f"prices {epic} {timeframe}",
            )
            prices = data.get("prices", [])
            if not prices:
                break
            batch = [Candle.from_capital(raw) for raw in prices]
            batch = [c for c in batch if c.ts is not None]
            collected = batch + collected
            remaining -= len(batch)
            if len(prices) < page:
                break
            earliest = batch[0].ts
            new_to = earliest.strftime("%Y-%m-%dT%H:%M:%S")
            if new_to == to_param:  # no progress; stop rather than loop forever
                break
            to_param = new_to
        collected.sort(key=lambda candle: candle.ts)
        # De-duplicate overlapping pages, keeping the latest version of a bar.
        unique: Dict[Any, Candle] = {candle.ts: candle for candle in collected}
        return [unique[key] for key in sorted(unique)][-limit:]

    # ------------------------------------------------------------------ positions

    def positions(self) -> List[BrokerPosition]:
        self.ensure_session()
        data = self._request("GET", "/api/v1/positions")
        return [BrokerPosition.from_capital(raw) for raw in data.get("positions", [])]

    def position(self, deal_id: str) -> Optional[BrokerPosition]:
        self.ensure_session()
        try:
            data = self._request("GET", f"/api/v1/positions/{deal_id}")
        except PermanentError as exc:
            if exc.status == 404 or "not" in str(exc.code or "").lower():
                return None
            raise
        if not data:
            return None
        return BrokerPosition.from_capital(data)

    def modify_position(
        self,
        deal_id: str,
        *,
        stop_level: Optional[float] = None,
        profit_level: Optional[float] = None,
    ) -> str:
        if stop_level is None and profit_level is None:
            raise ValueError("modify_position needs a stop_level or a profit_level")
        self.ensure_session()
        body: Dict[str, Any] = {}
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        data = self._request(
            "PUT", f"/api/v1/positions/{deal_id}", json=body,
            description=f"modify {deal_id}",
        )
        return data.get("dealReference", "")

    def close_position(self, deal_id: str, size: Optional[float] = None) -> str:
        self.ensure_session()
        body = {"size": size} if size is not None else None
        data = self._request(
            "DELETE", f"/api/v1/positions/{deal_id}", json=body,
            description=f"close {deal_id}",
        )
        return data.get("dealReference", "")

    def open_position(
        self,
        epic: str,
        direction: Direction,
        size: float,
        *,
        stop_level: Optional[float] = None,
        profit_level: Optional[float] = None,
    ) -> str:
        self.ensure_session()
        body: Dict[str, Any] = {
            "epic": epic,
            "direction": direction.value,
            "size": size,
        }
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        data = self._request(
            "POST", "/api/v1/positions", json=body,
            description=f"open {direction.value} {size} {epic}",
        )
        return data.get("dealReference", "")

    def confirm(self, deal_reference: str) -> Dict[str, Any]:
        self.ensure_session()
        return self._request("GET", f"/api/v1/confirms/{deal_reference}")

    # ------------------------------------------------------------------ capability probe

    def probe_partial_close(self) -> PartialCloseProbe:
        """Decide how partial closes will be executed on this account.

        Netting-offset wins whenever it is available.  A ``DELETE`` that quietly
        ignores ``size`` closes the entire position, so it is only chosen when
        netting is unavailable (hedging accounts) or explicitly configured.
        """
        notes: List[str] = []
        configured = self.config.partial_close_strategy.lower()
        hedging = self.hedging_mode()
        notes.append(
            f"account hedgingMode = {hedging}"
            if hedging is not None
            else "account hedgingMode = unknown (preferences unreadable)"
        )

        delete_accepts_size = self._probe_delete_size(notes)

        if configured in ("delete_with_size", "netting_offset"):
            strategy = PartialCloseStrategy(configured.upper())
            notes.append(f"strategy pinned by config to {strategy.value}")
        elif hedging is False:
            strategy = PartialCloseStrategy.NETTING_OFFSET
            notes.append("netting account: offset deal reduces the position deterministically")
        elif delete_accepts_size:
            strategy = PartialCloseStrategy.DELETE_WITH_SIZE
            notes.append("hedging/unknown account: falling back to DELETE with size")
        else:
            strategy = PartialCloseStrategy.UNSUPPORTED
            notes.append(
                "no safe partial-close path: enable netting (hedgingMode=false) "
                "or pin management.ladder to [] to disable partials"
            )

        self.partial_close_strategy = strategy
        return PartialCloseProbe(
            strategy=strategy,
            hedging_mode=hedging,
            delete_accepts_size=delete_accepts_size,
            notes=notes,
        )

    def _probe_delete_size(self, notes: List[str]) -> Optional[bool]:
        """Send a sized DELETE for a deal id that cannot exist.

        A *validation* rejection means the body shape is wrong; a *not found*
        rejection means the body was accepted and only the deal was missing.
        No real position is ever touched by this call.
        """
        try:
            self.close_position(_PROBE_DEAL_ID, size=0.01)
        except PermanentError as exc:
            code = str(exc.code or "").lower()
            if "not" in code and "found" in code.replace(".", " "):
                notes.append(f"DELETE {{dealId}} with size ... {exc.code} (body accepted)")
                return True
            notes.append(f"DELETE {{dealId}} with size ... {exc.code} (body rejected)")
            return False
        except RetryableError as exc:
            notes.append(f"DELETE {{dealId}} with size ... probe inconclusive ({exc})")
            return None
        notes.append("DELETE {dealId} with size ... unexpectedly succeeded on a fake deal id")
        return None
