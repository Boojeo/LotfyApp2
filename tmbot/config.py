"""Configuration: a YAML file for behaviour, environment variables for secrets.

Nothing sensitive ever belongs in the YAML -- credentials are read from the
environment (or a ``.env`` file) so the config can be committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ConfigError

LIVE_BASE_URL = "https://api-capital.backend-capital.com"
DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"


@dataclass
class BrokerConfig:
    environment: str = ""            # "demo" | "live" -- required, never defaulted
    api_key: str = ""
    identifier: str = ""             # account email
    password: str = ""
    account_id: str = ""             # optional: switch to this account on connect
    timeout: float = 20.0
    retry_attempts: int = 5
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    circuit_threshold: int = 5
    circuit_cooldown: float = 90.0
    keepalive_seconds: float = 240.0  # session tokens idle out around 10 min
    partial_close_strategy: str = "probe"  # probe | delete_with_size | netting_offset

    @property
    def base_url(self) -> str:
        if self.environment == "live":
            return LIVE_BASE_URL
        if self.environment == "demo":
            return DEMO_BASE_URL
        raise ConfigError("broker.environment must be explicitly set to 'demo' or 'live'")


@dataclass
class LadderStep:
    stage: str
    fraction: float  # fraction OF THE ORIGINAL position size


@dataclass
class ManagementConfig:
    poll_seconds: float = 10.0
    management_timeframe: str = "M15"
    management_lookback: int = 300

    # How the position is taken off:
    #   partial_close -- you open ONE deal; it is sliced by `ladder`.
    #   three_deals   -- you open THREE deals; each is closed whole at its own
    #                    target (leg 1 at TP1, leg 2 at TP2, leg 3 at TP3).
    #                    Requires hedging mode on the account, otherwise the
    #                    broker nets the three deals into a single position.
    exit_model: str = "three_deals"
    leg_targets: List[str] = field(default_factory=lambda: ["TP1", "TP2", "TP3"])
    group_window_minutes: float = 15.0
    # 50% of the original at TP1, 25% at TP2, the remaining 25% rides to TP3.
    ladder: List[LadderStep] = field(
        default_factory=lambda: [LadderStep("TP1", 0.50), LadderStep("TP2", 0.25)]
    )
    breakeven_stage: str = "TP1"
    breakeven_offset_r: float = 0.0   # 0.0 == stop sits exactly on the entry price
    trail_after_stage: str = "TP1"    # TP1 | TP2 | ENTRY | NEVER
    trail_atr_period: int = 14
    trail_k_strong: float = 2.5
    trail_k_moderate: float = 3.0
    trail_when_weak: bool = False     # a weak trend keeps the stop where it is
    structure_buffer_atr: float = 0.25
    swing_left: int = 2
    swing_right: int = 2
    adx_strong: float = 25.0
    adx_moderate: float = 18.0
    min_stop_improvement_atr: float = 0.10  # don't spam the API with 1-tick nudges
    extend_tp3: bool = True
    tp3_extension_atr: float = 1.0
    tp3_extension_trigger_atr: float = 0.5
    tp3_max_extensions: int = 3
    on_indivisible_size: str = "hold"  # hold | close_all
    max_quote_age_seconds: float = 90.0
    adoption_confirm_timeout_minutes: float = 30.0
    auto_decline_on_timeout: bool = True


@dataclass
class EpicConfig:
    epic: str
    display: str = ""
    news_query: str = ""
    timeframes: List[str] = field(default_factory=lambda: ["H4", "H1"])


@dataclass
class AnalysisConfig:
    watchlist: List[EpicConfig] = field(default_factory=list)
    structure_timeframe: str = "H4"
    structure_lookback: int = 400
    entry_timeframe: str = "H1"
    entry_lookback: int = 400
    atr_period: int = 14
    zone_tolerance_atr: float = 0.5
    min_target_separation_atr: float = 0.75
    min_first_target_atr: float = 0.6
    max_stop_atr: float = 2.5
    min_stop_atr: float = 0.6
    fallback_target_atr: List[float] = field(default_factory=lambda: [1.0, 1.8, 3.0])
    fallback_stop_atr: float = 1.2
    min_reward_risk: float = 0.8
    news_lookback_hours: int = 24
    news_limit: int = 12


@dataclass
class NewsConfig:
    provider: str = "none"   # marketaux | finnhub | none
    api_key: str = ""
    timeout: float = 15.0


@dataclass
class LLMConfig:
    enabled: bool = True
    model: str = "claude-opus-5"
    api_key: str = ""
    max_tokens: int = 1200
    timeout: float = 60.0


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    poll_seconds: float = 3.0
    timeout: float = 30.0


@dataclass
class ReportConfig:
    daily_time: str = "07:00"          # local time, HH:MM
    timezone: str = "UTC"
    intraday_refresh_hours: float = 4.0
    notify_on_bias_flip: bool = True
    notify_on_level_invalidated: bool = True
    output_dir: str = "reports"


@dataclass
class Config:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    management: ManagementConfig = field(default_factory=ManagementConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    database: str = "tmbot.sqlite3"
    log_level: str = "INFO"
    dry_run: bool = False

    def epic_config(self, epic: str) -> EpicConfig:
        for item in self.analysis.watchlist:
            if item.epic.upper() == epic.upper():
                return item
        return EpicConfig(epic=epic, display=epic, news_query=epic)

    def validate(self) -> None:
        if self.broker.environment not in ("demo", "live"):
            raise ConfigError(
                "broker.environment must be 'demo' or 'live' -- pass --env on the command line"
            )
        missing = [
            name for name, value in (
                ("CAPITAL_API_KEY", self.broker.api_key),
                ("CAPITAL_IDENTIFIER", self.broker.identifier),
                ("CAPITAL_PASSWORD", self.broker.password),
            ) if not value
        ]
        if missing:
            raise ConfigError(f"missing broker credentials: {', '.join(missing)}")
        if self.management.exit_model not in ("partial_close", "three_deals"):
            raise ConfigError(
                "management.exit_model must be 'partial_close' or 'three_deals'"
            )
        if self.management.exit_model == "partial_close":
            total = sum(step.fraction for step in self.management.ladder)
            if total >= 1.0:
                raise ConfigError(
                    f"management.ladder closes {total:.0%} of the position before TP3; "
                    "leave something for the runner"
                )
        else:
            targets = self.management.leg_targets
            if not targets or any(t.upper() not in ("TP1", "TP2", "TP3") for t in targets):
                raise ConfigError(
                    "management.leg_targets must list TP1/TP2/TP3 in the order the "
                    "legs should be closed"
                )
        if self.telegram.enabled and not (self.telegram.bot_token and self.telegram.chat_id):
            raise ConfigError("telegram.enabled is true but bot_token/chat_id are not set")
        if self.management.on_indivisible_size not in ("hold", "close_all"):
            raise ConfigError("management.on_indivisible_size must be 'hold' or 'close_all'")


def _coerce(cls: Any, raw: Any) -> Any:
    """Recursively build nested dataclasses from plain dicts."""
    if not is_dataclass(cls) or not isinstance(raw, dict):
        return raw
    kwargs: Dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in raw.items():
        if key not in known:
            raise ConfigError(f"unknown config key {key!r} in {cls.__name__}")
        field_type = known[key].type
        if key == "watchlist":
            kwargs[key] = [
                EpicConfig(**item) if isinstance(item, dict) else EpicConfig(epic=str(item))
                for item in value
            ]
        elif key == "ladder":
            kwargs[key] = [LadderStep(**item) for item in value]
        elif isinstance(value, dict) and isinstance(field_type, str) and field_type in _NESTED:
            kwargs[key] = _coerce(_NESTED[field_type], value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


_NESTED = {
    "BrokerConfig": BrokerConfig,
    "ManagementConfig": ManagementConfig,
    "AnalysisConfig": AnalysisConfig,
    "NewsConfig": NewsConfig,
    "LLMConfig": LLMConfig,
    "TelegramConfig": TelegramConfig,
    "ReportConfig": ReportConfig,
}


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Minimal ``.env`` loader -- existing environment variables always win."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def apply_env(config: Config) -> Config:
    """Overlay secrets from the environment onto a config."""
    env = os.environ
    config.broker.api_key = env.get("CAPITAL_API_KEY", config.broker.api_key)
    config.broker.identifier = env.get("CAPITAL_IDENTIFIER", config.broker.identifier)
    config.broker.password = env.get("CAPITAL_PASSWORD", config.broker.password)
    config.broker.account_id = env.get("CAPITAL_ACCOUNT_ID", config.broker.account_id)
    config.news.api_key = env.get("NEWS_API_KEY", config.news.api_key)
    config.llm.api_key = env.get("ANTHROPIC_API_KEY", config.llm.api_key)
    config.telegram.bot_token = env.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token)
    config.telegram.chat_id = env.get("TELEGRAM_CHAT_ID", config.telegram.chat_id)
    if config.telegram.bot_token and config.telegram.chat_id:
        config.telegram.enabled = True
    return config


def load(path: Optional[str] = None, *, env_file: str = ".env") -> Config:
    """Load YAML (or JSON) config, then overlay environment secrets."""
    load_dotenv(env_file)
    raw: Dict[str, Any] = {}
    if path:
        file = Path(path)
        if not file.is_file():
            raise ConfigError(f"config file not found: {path}")
        text = file.read_text(encoding="utf-8")
        if file.suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise ConfigError("PyYAML is required to read YAML config files") from exc
            raw = yaml.safe_load(text) or {}
        else:
            import json
            raw = json.loads(text)
    config = _coerce(Config, raw) if raw else Config()
    return apply_env(config)
