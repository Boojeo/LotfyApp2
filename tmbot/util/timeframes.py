"""Mapping between human timeframe names and Capital.com resolutions."""

from __future__ import annotations

from ..errors import ConfigError

# Capital.com /prices resolutions.
RESOLUTIONS: dict[str, str] = {
    "M1": "MINUTE",
    "M5": "MINUTE_5",
    "M15": "MINUTE_15",
    "M30": "MINUTE_30",
    "H1": "HOUR",
    "H4": "HOUR_4",
    "D1": "DAY",
    "W1": "WEEK",
}

SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}


def resolution(timeframe: str) -> str:
    key = timeframe.upper()
    if key not in RESOLUTIONS:
        raise ConfigError(f"unknown timeframe {timeframe!r}; expected one of {sorted(RESOLUTIONS)}")
    return RESOLUTIONS[key]


def seconds(timeframe: str) -> int:
    key = timeframe.upper()
    if key not in SECONDS:
        raise ConfigError(f"unknown timeframe {timeframe!r}")
    return SECONDS[key]
