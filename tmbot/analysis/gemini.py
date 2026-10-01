"""Gemini: an independent second opinion, shown beside the bot -- never in charge.

The bot's own analysis is the decision. Gemini receives the same market data
and the bot's levels, gives its own read (technical, and fundamental/news via
Google Search), and the two are shown side by side with an explicit
ALIGNED / CONFLICT / UNCERTAIN. Nothing here changes a level, a lot size, a
verdict, or a trade.

Every failure (timeout, bad key, rate limit, network, empty or malformed
answer) is caught and reported by name. A failed second opinion can only ever
mean "no second opinion" -- never a looser risk check or a silent approval.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..config import GeminiConfig
from ..models import Bias, Candle, TradePlan

log = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

OK = "OK"
DISABLED = "DISABLED"          # no API key configured
TIMEOUT = "TIMEOUT"
BAD_KEY = "BAD_KEY"
RATE_LIMITED = "RATE_LIMITED"
NETWORK = "NETWORK"
NO_ANSWER = "NO_ANSWER"        # empty, blocked or refused
MALFORMED = "MALFORMED"        # not the JSON we asked for
ERROR = "ERROR"                # anything else from the API

DIRECTIONS = ("BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN")
QUALITIES = ("GOOD", "WEAK", "POOR", "UNCERTAIN")
FUNDAMENTALS = ("BULLISH", "BEARISH", "NEUTRAL", "UNAVAILABLE")
NEWS = ("AVAILABLE", "UNAVAILABLE", "UNCERTAIN")
ALIGNMENTS = ("ALIGNED", "PARTIAL", "CONFLICT", "UNCERTAIN")

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "technical_direction": {"type": "string", "enum": list(DIRECTIONS)},
        "entry_quality": {"type": "string", "enum": list(QUALITIES)},
        "technical_strength": {"type": "integer", "minimum": 0, "maximum": 100},
        "market_structure": {"type": "string"},
        "timeframe_alignment": {"type": "string", "enum": list(ALIGNMENTS)},
        "fundamental_bias": {"type": "string", "enum": list(FUNDAMENTALS)},
        "news_status": {"type": "string", "enum": list(NEWS)},
        "news_summary": {"type": "string"},
        "key_reasons": {"type": "array", "items": {"type": "string"}},
        "risk_concerns": {"type": "array", "items": {"type": "string"}},
        "invalidating_conditions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "technical_direction", "entry_quality", "technical_strength",
        "market_structure", "timeframe_alignment", "fundamental_bias",
        "news_status", "key_reasons", "risk_concerns", "invalidating_conditions",
    ],
}

SYSTEM = (
    "You are an independent market analyst giving a second opinion on another "
    "system's analysis. Form your OWN view from the data; do not defer to the "
    "other system and do not agree just to agree -- a reasoned disagreement is "
    "valuable. Separate direction (which way the market leans) from entry "
    "quality (whether this price is a good place to join it). "
    "For fundamentals and news, use only current, verifiable information found "
    "by search. If you cannot find or verify recent news, set news_status to "
    "UNAVAILABLE and fundamental_bias to UNAVAILABLE -- never turn missing "
    "information into a bullish or bearish view, and never invent events. "
    "technical_strength is how strong the evidence is, not a probability of "
    "winning. Keep each list to at most 3 short items. Reply with JSON only."
)


def _bars_text(candles: List[Candle], digits: int) -> str:
    rows = [
        f"{c.ts:%Y-%m-%d %H:%M} O{c.open:.{digits}f} H{c.high:.{digits}f} "
        f"L{c.low:.{digits}f} C{c.close:.{digits}f}"
        for c in candles
    ]
    return "\n".join(rows)


def build_prompt(plan: TradePlan, candles: List[Candle], display: str, digits: int) -> str:
    technical = plan.technical
    style = plan.style or {}
    found = plan.assessment or {}
    sizing = plan.sizing or {}
    levels = sorted({round(level.price, digits) for level in plan.levels})
    lines = [
        f"Instrument: {display} ({plan.epic})",
        f"Current price: {plan.reference_price}",
        f"Entry timeframe: {style.get('entry', '')}; higher timeframe: "
        f"{style.get('structure', '')} (read there: {found.get('higher_bias') or 'n/a'})",
        f"Indicators on the entry timeframe: EMA20 {technical.get('ema_fast')}, "
        f"EMA50 {technical.get('ema_slow')}, EMA200 {technical.get('ema_trend')}, "
        f"RSI {technical.get('rsi')}, ADX {technical.get('adx')} "
        f"({technical.get('strength')}), ATR {plan.atr}",
        f"Support/resistance levels found: {', '.join(str(v) for v in levels) or 'none'}",
        "",
        "The other system's read (judge it independently):",
        f"  direction {plan.bias.value}, signal strength {plan.confidence:.0f}/100, "
        f"entry quality {found.get('quality', 'n/a')}, verdict {found.get('verdict', 'n/a')}",
        f"  plan side {plan.direction.value}: entry {plan.reference_price}, SL {plan.sl}, "
        f"TP1 {plan.tp1}, TP2 {plan.tp2}, TP3 {plan.tp3}",
        f"  risk: up to {sizing.get('risk_percent', '?')}% of equity for the whole trade "
        f"({sizing.get('verdict', 'unchecked')})",
        "",
        f"Last {len(candles)} {style.get('entry', '')} candles (oldest first):",
        _bars_text(candles, digits),
        "",
        "Give your own structured read, including current fundamental/news "
        "context for this instrument if you can verify it.",
    ]
    return "\n".join(lines)


def agreement(bot: Bias, gemini_direction: str) -> str:
    """ALIGNED / CONFLICT / UNCERTAIN -- a disagreement is kept, never averaged."""
    if gemini_direction not in ("BULLISH", "BEARISH", "NEUTRAL"):
        return "UNCERTAIN"
    if gemini_direction == bot.value:
        return "ALIGNED"
    if {gemini_direction, bot.value} == {"BULLISH", "BEARISH"}:
        return "CONFLICT"
    return "UNCERTAIN"


def failure(status: str, reason: str, model: str) -> Dict[str, Any]:
    return {"status": status, "reason": reason, "model": model}


class GeminiReviewer:
    def __init__(self, config: GeminiConfig, session: Optional[requests.Session] = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self._http = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._cache: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = {}
        # One question at a time: the daily report and a Telegram /now can
        # ask together, and a burst is exactly what trips the rate limit.
        self._lock = threading.Lock()
        self._last_call: Optional[float] = None
        self._paused_until = 0.0
        self._pause_reason = ""

    def review(self, plan: TradePlan, candles: List[Candle], *,
               display: str = "", digits: int = 2) -> Dict[str, Any]:
        """The second opinion for ``plan``, or the named reason there is none."""
        model = self.config.model
        if not self.config.active:
            return failure(DISABLED, "no GEMINI_API_KEY set", model)

        key = (plan.epic, plan.direction.value, str((plan.style or {}).get("name", "")))
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < self.config.cache_minutes * 60:
            return dict(cached[1], cached=True)

        prompt = build_prompt(plan, candles[-self.config.bars:], display or plan.epic, digits)
        with self._lock:
            now = self._clock()
            if now < self._paused_until:
                # Asking again inside the limit only fails again and can push
                # a per-minute block into a longer one. Say when it resumes.
                wait = int(self._paused_until - now) + 1
                return failure(RATE_LIMITED,
                               f"{self._pause_reason} -- paused, next try in {wait}s", model)
            if self._last_call is not None:
                gap = self._last_call + self.config.min_interval_seconds - now
                if gap > 0:
                    self._sleep(min(gap, self.config.min_interval_seconds))
            result = self._ask(prompt)
            self._last_call = self._clock()
            if result.get("status") == RATE_LIMITED:
                self._paused_until = self._clock() + float(result.pop("pause_seconds", 60.0))
                self._pause_reason = result.get("reason", "rate limited")
        if result.get("status") == OK:
            result["agreement"] = agreement(plan.bias, result.get("technical_direction", ""))
            self._cache[key] = (self._clock(), result)
        return result

    # ------------------------------------------------------------------ transport

    def _body(self, prompt: str, schema: bool) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        if self.config.search_news:
            body["tools"] = [{"google_search": {}}]
        if schema:
            body["generationConfig"] = {
                "responseMimeType": "application/json",
                "responseJsonSchema": SCHEMA,
            }
        return body

    def _ask(self, prompt: str) -> Dict[str, Any]:
        model = self.config.model
        url = ENDPOINT.format(model=model)
        # The key travels in a header, never in the URL, so no error message
        # or log line can ever contain it.
        headers = {"x-goog-api-key": self.config.api_key, "Content-Type": "application/json"}
        for schema in (True, False):
            try:
                response = self._http.post(
                    url, json=self._body(prompt, schema), headers=headers,
                    timeout=self.config.timeout,
                )
            except requests.Timeout:
                return failure(TIMEOUT, f"no answer within {self.config.timeout:.0f}s", model)
            except requests.RequestException as exc:
                return failure(NETWORK, type(exc).__name__, model)

            if response.status_code == 400 and schema and _schema_rejected(response):
                continue  # this model will not take a schema with search; ask plainly
            if response.status_code >= 400:
                return _http_failure(response, model)
            return _parse(response, model)
        return failure(ERROR, "request rejected", model)


def _error_text(response: requests.Response) -> str:
    try:
        error = response.json().get("error", {})
        return f"{error.get('status', '')} {error.get('message', '')}".strip()
    except ValueError:
        return response.text[:200]


def _schema_rejected(response: requests.Response) -> bool:
    text = _error_text(response).lower()
    return "schema" in text or "mime" in text or "tool" in text


def _http_failure(response: requests.Response, model: str) -> Dict[str, Any]:
    code = response.status_code
    text = _error_text(response)
    if code in (401, 403) or "api key" in text.lower() or "API_KEY_INVALID" in text:
        return failure(BAD_KEY, "the API key was refused -- check GEMINI_API_KEY", model)
    if code == 429:
        reason, pause = _quota_reason(response, model)
        return dict(failure(RATE_LIMITED, reason, model), pause_seconds=pause)
    if code == 404:
        return failure(ERROR, f"model {model!r} not found -- check gemini.model", model)
    return failure(ERROR, f"HTTP {code} {text[:120]}", model)


def _quota_reason(response: requests.Response, model: str) -> Tuple[str, float]:
    """Which limit was hit, in words, and how long to stay quiet.

    Google says which quota (per minute, per day, or none at all on the free
    tier) and often when to retry; that decides between "wait a minute" and
    "this will not work today -- change the model or turn on billing".
    """
    try:
        error = response.json().get("error", {}) or {}
    except ValueError:
        error = {}
    message = str(error.get("message", ""))
    retry: Optional[float] = None
    quotas: List[str] = []
    for detail in error.get("details") or []:
        kind = str(detail.get("@type", ""))
        if kind.endswith("RetryInfo"):
            found = re.match(r"([\d.]+)s", str(detail.get("retryDelay", "")))
            if found:
                retry = float(found.group(1))
        if kind.endswith("QuotaFailure"):
            for violation in detail.get("violations") or []:
                quotas.append(str(violation.get("quotaId") or violation.get("quotaMetric") or ""))
    if retry is None:
        found = re.search(r"retry in ([\d.]+)\s*s", message, flags=re.IGNORECASE)
        retry = float(found.group(1)) if found else None
    seen = " ".join(quotas + [message]).lower()

    if re.search(r"limit:\s*0\b", message):
        return (f"model {model} has no free quota on this key (limit 0) -- set "
                "gemini.model to another model, or turn on billing in Google AI Studio",
                3600.0)
    if "perday" in seen or "per_day" in seen or "per day" in seen or "daily" in seen:
        return ("the daily free quota is used up -- it resets every 24h; or turn on "
                "billing in Google AI Studio", max(retry or 0.0, 3600.0))
    if "search" in seen or "grounding" in seen:
        return ("the Google Search (news) quota is used up -- set gemini.search_news: "
                "false to keep the technical second opinion", max(retry or 0.0, 3600.0))
    return ("the per-minute limit was reached", retry if retry else 60.0)


def _parse(response: requests.Response, model: str) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return failure(MALFORMED, "the reply was not JSON", model)

    block = (data.get("promptFeedback") or {}).get("blockReason")
    candidates = data.get("candidates") or []
    if block or not candidates:
        return failure(NO_ANSWER, f"no answer ({block or 'empty reply'})", model)
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    if not text.strip():
        reason = candidate.get("finishReason") or "empty reply"
        return failure(NO_ANSWER, f"no answer ({reason})", model)

    payload = _extract_json(text)
    if payload is None:
        return failure(MALFORMED, "the reply was not the structured answer asked for", model)
    result = _validate(payload)
    if result is None:
        return failure(MALFORMED, "the reply was missing required fields", model)

    sources = _sources(candidate)
    if not sources:
        # Nothing was actually searched, so any news claim is unverified.
        result["news_status"] = "UNAVAILABLE"
        result["fundamental_bias"] = "UNAVAILABLE"
        result["news_summary"] = ""
    result.update(status=OK, model=model, sources=sources)
    return result


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        value = json.loads(cleaned)
    except ValueError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _validate(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Keep only well-formed fields; out-of-range answers become UNCERTAIN."""
    def choice(name: str, allowed: tuple, fallback: str) -> str:
        value = str(payload.get(name, "")).upper().strip()
        return value if value in allowed else fallback

    def items(name: str) -> List[str]:
        raw = payload.get(name) or []
        if not isinstance(raw, list):
            return []
        return [str(item)[:160] for item in raw[:3] if str(item).strip()]

    if "technical_direction" not in payload:
        return None
    try:
        strength = max(0, min(100, int(float(payload.get("technical_strength", 0)))))
    except (TypeError, ValueError):
        strength = 0
    return {
        "technical_direction": choice("technical_direction", DIRECTIONS, "UNCERTAIN"),
        "entry_quality": choice("entry_quality", QUALITIES, "UNCERTAIN"),
        "technical_strength": strength,
        "market_structure": str(payload.get("market_structure", ""))[:200],
        "timeframe_alignment": choice("timeframe_alignment", ALIGNMENTS, "UNCERTAIN"),
        "fundamental_bias": choice("fundamental_bias", FUNDAMENTALS, "UNAVAILABLE"),
        "news_status": choice("news_status", NEWS, "UNAVAILABLE"),
        "news_summary": str(payload.get("news_summary", ""))[:300],
        "key_reasons": items("key_reasons"),
        "risk_concerns": items("risk_concerns"),
        "invalidating_conditions": items("invalidating_conditions"),
    }


def _sources(candidate: Dict[str, Any]) -> List[Dict[str, str]]:
    chunks = (candidate.get("groundingMetadata") or {}).get("groundingChunks") or []
    found = []
    for chunk in chunks:
        web = chunk.get("web") or {}
        if web.get("uri"):
            found.append({"title": str(web.get("title", ""))[:80], "uri": web["uri"]})
    return found[:3]
