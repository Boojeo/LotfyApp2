"""News providers.

Two implementations plus a null object, behind one interface, so the daily
report degrades to a purely technical read instead of failing when a news
vendor is down or unconfigured.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any, Dict, List

import requests

from ..config import NewsConfig
from ..errors import RetryableError
from ..models import utcnow

log = logging.getLogger(__name__)

Headline = Dict[str, Any]


class NewsProvider(ABC):
    name = "none"

    @abstractmethod
    def fetch(self, query: str, *, hours: int = 24, limit: int = 12) -> List[Headline]:
        ...


class NullNewsProvider(NewsProvider):
    def fetch(self, query: str, *, hours: int = 24, limit: int = 12) -> List[Headline]:
        return []


class _HttpNewsProvider(NewsProvider):
    def __init__(self, config: NewsConfig):
        self.config = config
        self._http = requests.Session()

    def _get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self._http.get(url, params=params, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise RetryableError(f"{self.name} request failed: {exc}") from exc
        if response.status_code == 429:
            raise RetryableError(f"{self.name} rate limited")
        if response.status_code >= 500:
            raise RetryableError(f"{self.name} server error {response.status_code}")
        response.raise_for_status()
        return response.json()


class MarketauxProvider(_HttpNewsProvider):
    name = "marketaux"

    def fetch(self, query: str, *, hours: int = 24, limit: int = 12) -> List[Headline]:
        published_after = (utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
        data = self._get(
            "https://api.marketaux.com/v1/news/all",
            {
                "api_token": self.config.api_key,
                "search": query,
                "published_after": published_after,
                "language": "en",
                "limit": min(limit, 50),
            },
        )
        return [
            {
                "title": item.get("title", ""),
                "summary": item.get("description", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "published_at": item.get("published_at", ""),
                "sentiment": _first_entity_sentiment(item),
            }
            for item in data.get("data", [])
        ][:limit]


def _first_entity_sentiment(item: Dict[str, Any]) -> Any:
    for entity in item.get("entities", []) or []:
        score = entity.get("sentiment_score")
        if score is not None:
            return score
    return None


class FinnhubProvider(_HttpNewsProvider):
    name = "finnhub"

    def fetch(self, query: str, *, hours: int = 24, limit: int = 12) -> List[Headline]:
        now = utcnow()
        data = self._get(
            "https://finnhub.io/api/v1/news",
            {"token": self.config.api_key, "category": "forex", "minId": 0},
        )
        cutoff = (now - timedelta(hours=hours)).timestamp()
        terms = [term.lower() for term in query.split() if len(term) > 2]
        headlines: List[Headline] = []
        for item in data if isinstance(data, list) else []:
            if item.get("datetime", 0) < cutoff:
                continue
            haystack = f"{item.get('headline', '')} {item.get('summary', '')}".lower()
            if terms and not any(term in haystack for term in terms):
                continue
            headlines.append({
                "title": item.get("headline", ""),
                "summary": item.get("summary", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "published_at": item.get("datetime", ""),
                "sentiment": None,
            })
            if len(headlines) >= limit:
                break
        return headlines


def build(config: NewsConfig) -> NewsProvider:
    provider = (config.provider or "none").lower()
    if provider == "none":
        return NullNewsProvider()
    if not config.api_key:
        log.warning("news provider %r configured without an API key; disabling news", provider)
        return NullNewsProvider()
    if provider == "marketaux":
        return MarketauxProvider(config)
    if provider == "finnhub":
        return FinnhubProvider(config)
    log.warning("unknown news provider %r; disabling news", provider)
    return NullNewsProvider()
