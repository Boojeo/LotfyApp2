"""Fundamental read: headlines synthesised into a directional view by Claude.

The LLM never sees the technical score and is never asked for price levels --
it reads the news and returns a bias, drivers and risks.  Levels stay with the
deterministic structure code so a hallucinated number can never become a stop.

If the Anthropic SDK or key is unavailable, a transparent lexicon fallback runs
instead, so the daily report always has a fundamental section.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from ..config import LLMConfig
from ..models import Bias

log = logging.getLogger(__name__)

_BULLISH_TERMS = (
    "rally", "surge", "gains", "jumps", "climbs", "record high", "upgrade",
    "beats", "stronger", "demand", "inflows", "hawkish", "cuts", "stimulus",
    "safe haven", "rebound", "optimism",
)
_BEARISH_TERMS = (
    "falls", "slides", "drops", "plunge", "selloff", "downgrade", "misses",
    "weaker", "outflows", "dovish", "hike", "recession", "slump", "pressure",
    "profit taking", "correction", "fears",
)

_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "bias": {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "drivers": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "catalysts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["bias", "confidence", "summary", "drivers", "risks", "catalysts"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a markets analyst summarising news flow for one instrument. "
    "Judge only the directional pull of the supplied headlines over the next "
    "24 hours. Do not invent price levels, targets or stops, and do not give "
    "trading advice. If the headlines are thin or contradictory, say NEUTRAL "
    "with low confidence rather than forcing a view."
)


@dataclass
class FundamentalRead:
    bias: Bias
    confidence: float
    summary: str
    drivers: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    catalysts: List[str] = field(default_factory=list)
    source: str = "lexicon"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bias": self.bias.value,
            "confidence": round(self.confidence, 1),
            "summary": self.summary,
            "drivers": self.drivers,
            "risks": self.risks,
            "catalysts": self.catalysts,
            "source": self.source,
        }


def _lexicon_read(headlines: Sequence[Dict[str, Any]]) -> FundamentalRead:
    """Deterministic fallback: keyword polarity plus any vendor sentiment."""
    if not headlines:
        return FundamentalRead(
            bias=Bias.NEUTRAL,
            confidence=0.0,
            summary="No headlines available; fundamental input excluded from the bias.",
            source="none",
        )
    score = 0.0
    counted = 0
    drivers: List[str] = []
    for item in headlines:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        hits = sum(term in text for term in _BULLISH_TERMS)
        hits -= sum(term in text for term in _BEARISH_TERMS)
        vendor = item.get("sentiment")
        if vendor is not None:
            try:
                hits += float(vendor) * 2
            except (TypeError, ValueError):
                pass
        if hits:
            counted += 1
            score += max(-2.0, min(2.0, float(hits)))
            if len(drivers) < 4:
                drivers.append(item.get("title", "")[:160])
    if not counted:
        return FundamentalRead(
            bias=Bias.NEUTRAL,
            confidence=10.0,
            summary=f"{len(headlines)} headlines, none with a clear directional tilt.",
            source="lexicon",
        )
    average = score / counted
    bias = Bias.BULLISH if average > 0.4 else Bias.BEARISH if average < -0.4 else Bias.NEUTRAL
    return FundamentalRead(
        bias=bias,
        confidence=min(60.0, abs(average) * 30.0 + counted * 2.0),
        summary=(
            f"Keyword read of {len(headlines)} headlines "
            f"({counted} directional) scores {average:+.2f}."
        ),
        drivers=drivers,
        risks=["Keyword fallback in use: no language model available."],
        source="lexicon",
    )


def analyse(
    epic: str,
    display_name: str,
    headlines: Sequence[Dict[str, Any]],
    config: LLMConfig,
) -> FundamentalRead:
    """Synthesise ``headlines`` into a fundamental view."""
    if not config.enabled or not headlines:
        return _lexicon_read(headlines)
    try:
        return _claude_read(display_name or epic, headlines, config)
    except Exception as exc:  # the report must never die on a news summary
        log.warning("Claude fundamental read failed (%s); using lexicon fallback", exc)
        read = _lexicon_read(headlines)
        read.risks.append(f"LLM synthesis unavailable: {exc}")
        return read


def _claude_read(
    instrument: str,
    headlines: Sequence[Dict[str, Any]],
    config: LLMConfig,
) -> FundamentalRead:
    import anthropic  # imported lazily so the package is optional

    client = (
        anthropic.Anthropic(api_key=config.api_key, timeout=config.timeout)
        if config.api_key
        else anthropic.Anthropic(timeout=config.timeout)
    )
    lines = [
        f"- [{item.get('published_at', '')}] {item.get('source', '')}: "
        f"{item.get('title', '')} -- {(item.get('summary') or '')[:400]}"
        for item in headlines
    ]
    prompt = (
        f"Instrument: {instrument}\n"
        f"Headlines from the last 24 hours ({len(lines)}):\n"
        + "\n".join(lines)
        + "\n\nReturn the structured fundamental read."
    )

    try:
        response = client.messages.create(
            model=config.model,
            max_tokens=config.max_tokens,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except anthropic.RateLimitError as exc:
        raise RuntimeError(f"rate limited: {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"connection error: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"API error {exc.status_code}: {exc.message}") from exc

    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("model declined to answer")

    text = next((block.text for block in response.content if block.type == "text"), "")
    payload = json.loads(text)
    return FundamentalRead(
        bias=Bias(payload["bias"]),
        confidence=float(payload["confidence"]),
        summary=payload["summary"],
        drivers=list(payload.get("drivers", [])),
        risks=list(payload.get("risks", [])),
        catalysts=list(payload.get("catalysts", [])),
        source=f"claude:{config.model}",
    )
