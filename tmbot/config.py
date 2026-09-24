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
from .i18n import LANGUAGES

LIVE_BASE_URL = "https://api-capital.backend-capital.com"
DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"


@dataclass
class AccountCredentials:
    """One Capital.com account.  Demo and live are issued separate API keys."""

    api_key: str = ""
    identifier: str = ""             # account email
    password: str = ""
    account_id: str = ""             # optional: switch to this sub-account

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.identifier and self.password)


@dataclass
class MT5Account:
    """One MetaTrader 5 account (Exness or any other MT5 broker).

    The number, password and server name are exactly what you type into the
    MT5 login window. The password is the TRADING password, not the one for
    the broker's website.
    """

    login: str = ""
    password: str = ""
    server: str = ""                 # e.g. Exness-MT5Trial8 -- shown at login
    # Optional: the full path to terminal64.exe. Only needed when more than one
    # MetaTrader 5 is installed, so the right one is started.
    terminal_path: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.login and self.password and self.server)


@dataclass
class MT5Config:
    demo: MT5Account = field(default_factory=MT5Account)
    live: MT5Account = field(default_factory=MT5Account)
    # MT5 stamps positions and bars in the broker's server time, not UTC.
    # Exness runs its servers on UTC, so 0 is right there; other brokers are
    # often +2 or +3.
    server_utc_offset_hours: float = 0.0
    # How far (in points) the fill may slip from the quoted price on a close.
    deviation_points: int = 30
    # How long to wait for the terminal to start and log in.
    connect_timeout_ms: int = 60000


@dataclass
class BrokerConfig:
    environment: str = ""            # "demo" | "live" -- required, never defaulted
    # Which kind of broker connection to use:
    #   capital -- Capital.com's REST API (the original setup)
    #   mt5     -- a MetaTrader 5 terminal on this Windows PC (Exness, ...)
    platform: str = "capital"
    mt5: MT5Config = field(default_factory=MT5Config)
    # Flat fields are the shared fallback, so a single-account setup keeps
    # working untouched. Per-environment credentials override them.
    api_key: str = ""
    identifier: str = ""             # account email
    password: str = ""
    account_id: str = ""
    demo: AccountCredentials = field(default_factory=AccountCredentials)
    live: AccountCredentials = field(default_factory=AccountCredentials)
    # Live must be switched on deliberately, in the config file, as well as
    # chosen on the command line. Two separate acts, neither accidental.
    live_enabled: bool = False
    timeout: float = 20.0
    retry_attempts: int = 5
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    circuit_threshold: int = 5
    circuit_cooldown: float = 90.0
    keepalive_seconds: float = 240.0  # session tokens idle out around 10 min
    partial_close_strategy: str = "probe"  # probe | delete_with_size | netting_offset

    @property
    def active(self) -> AccountCredentials:
        """Credentials for the environment currently selected."""
        chosen = self.live if self.environment == "live" else self.demo
        return AccountCredentials(
            api_key=chosen.api_key or self.api_key,
            identifier=chosen.identifier or self.identifier,
            password=chosen.password or self.password,
            account_id=chosen.account_id or self.account_id,
        )

    @property
    def active_mt5(self) -> MT5Account:
        """MetaTrader 5 login for the environment currently selected."""
        return self.mt5.live if self.environment == "live" else self.mt5.demo

    @property
    def credentials_ready(self) -> bool:
        """Whether the selected platform has a full login for this environment."""
        if self.platform == "mt5":
            return self.active_mt5.configured
        return self.active.configured

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
class ReversalConfig:
    """Trend-reversal failsafe.

    The bot never opens a position to recover -- no hedging, no averaging down.
    It protects what is already there: tighten, take profit early, or tell you.
    """

    enabled: bool = True
    min_signals: int = 2          # how many independent signals must agree
    confirm_cycles: int = 2       # consecutive detections before acting
    adx_min: float = 20.0         # below this the market has no trend to reverse
    action: str = "tighten"       # tighten | close | alert
    tighten_atr: float = 1.0      # pull the stop to this many ATR behind price
    min_r_to_act: float = 0.0     # only act once the trade is this profitable
    cooldown_minutes: float = 60.0
    alert_only_below_min_r: bool = True


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
    charts: bool = True
    chart_theme: str = "dark"        # light | dark
    chart_timeframe: str = "H1"
    chart_bars: int = 120


@dataclass
class Config:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    management: ManagementConfig = field(default_factory=ManagementConfig)
    reversal: ReversalConfig = field(default_factory=ReversalConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    database: str = "tmbot.sqlite3"
    log_level: str = "INFO"
    dry_run: bool = False
    # Display language for alerts, commands and reports. Stored records and log
    # lines stay English so they remain greppable and portable.
    language: str = "en"

    def _per_environment(self, value: str) -> str:
        """Suffix a path with the environment.

        Not optional, and not a setting. Sharing one database between demo and
        live blends practice results into the journal and makes the only
        measure of whether this works meaningless.
        """
        environment = self.broker.environment or "unset"
        path = Path(value)
        if path.suffix:
            return str(path.with_name(f"{path.stem}-{environment}{path.suffix}"))
        return str(path / environment)

    @property
    def resolved_database(self) -> str:
        return self._per_environment(self.database)

    @property
    def resolved_report_dir(self) -> str:
        return self._per_environment(self.report.output_dir)

    def canonical_epic(self, text: str) -> str:
        """The instrument name as the broker spells it, from what was typed.

        Capital.com epics are upper case. MetaTrader 5 names are
        case-sensitive (Exness: XAUUSDm), so there the watchlist spelling wins
        and anything else is passed through untouched.
        """
        text = text.strip()
        if self.broker.platform != "mt5":
            return text.upper()
        for item in self.analysis.watchlist:
            if item.epic.upper() == text.upper():
                return item.epic
        return text

    def epic_config(self, epic: str) -> EpicConfig:
        for item in self.analysis.watchlist:
            if item.epic.upper() == epic.upper():
                return item
        return EpicConfig(epic=epic, display=epic, news_query=epic)

    def validate(self, *, connecting: bool = True) -> None:
        """Check the configuration.

        ``connecting`` is False for commands that only read settings back, so
        a missing key or a disabled live account is reported by the command
        itself rather than stopping you from looking at it.
        """
        if self.broker.environment not in ("demo", "live"):
            raise ConfigError(
                "broker.environment must be 'demo' or 'live' -- pass --env on the command line"
            )
        if self.broker.platform not in ("capital", "mt5"):
            raise ConfigError("broker.platform must be 'capital' or 'mt5'")
        environment = self.broker.environment.upper()
        if self.broker.platform == "mt5":
            login = self.broker.active_mt5
            required = (
                (f"MT5_{environment}_LOGIN", login.login),
                (f"MT5_{environment}_PASSWORD", login.password),
                (f"MT5_{environment}_SERVER", login.server),
            )
            if login.login and not str(login.login).strip().isdigit():
                raise ConfigError(
                    f"MT5_{environment}_LOGIN must be the account NUMBER shown in "
                    f"MetaTrader 5, got {login.login!r}"
                )
        else:
            active = self.broker.active
            required = (
                (f"CAPITAL_{environment}_API_KEY", active.api_key),
                (f"CAPITAL_{environment}_IDENTIFIER", active.identifier),
                (f"CAPITAL_{environment}_PASSWORD", active.password),
            )
        missing = [name for name, value in required if not value]
        if missing and connecting:
            raise ConfigError(
                f"no {self.broker.environment} credentials: set "
                f"{', '.join(missing)} in .env"
            )
        if connecting and self.broker.environment == "live" and not self.broker.live_enabled:
            raise ConfigError(
                "live trading is switched off. Set broker.live_enabled: true in "
                "your config file to enable it -- choosing --env live alone is "
                "deliberately not enough."
            )
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
        if self.reversal.action not in ("tighten", "close", "alert"):
            raise ConfigError(
                "reversal.action must be 'tighten', 'close' or 'alert' -- the bot "
                "never opens a position to recover"
            )
        # Resolved here so a bad or unavailable timezone is reported by
        # `check`, not discovered when a report arrives at the wrong hour.
        resolve_timezone(self.report.timezone)
        try:
            hour, minute = (int(part) for part in self.report.daily_time.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except ValueError:
            raise ConfigError(
                f"report.daily_time must be HH:MM, got {self.report.daily_time!r}"
            ) from None
        if self.report.chart_theme not in ("light", "dark"):
            raise ConfigError("report.chart_theme must be 'light' or 'dark'")
        if self.language not in LANGUAGES:
            raise ConfigError(
                f"language must be one of {', '.join(LANGUAGES)}, got {self.language!r}"
            )
        if self.management.on_indivisible_size not in ("hold", "close_all"):
            raise ConfigError("management.on_indivisible_size must be 'hold' or 'close_all'")


def resolve_timezone(name: str):
    """Turn a timezone name into a tzinfo, or say precisely why it cannot.

    Windows ships no timezone database, so ``ZoneInfo("Asia/Riyadh")`` fails
    there unless the ``tzdata`` package is installed. Falling back to UTC with
    only a log line would send the daily report at the wrong hour every day and
    look like it was working.
    """
    from datetime import timezone as _timezone
    if name.upper() == "UTC":
        return _timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError as exc:  # pragma: no cover - Python < 3.9
        raise ConfigError(f"timezone support unavailable: {exc}") from exc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"unknown timezone {name!r}. On Windows the timezone database is "
            "not built in -- run: pip install tzdata  (it is in "
            "requirements.txt). Otherwise check the spelling, e.g. Asia/Riyadh."
        ) from exc
    except (ValueError, OSError) as exc:
        raise ConfigError(f"invalid timezone {name!r}: {exc}") from exc


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
        elif key in ("demo", "live") and isinstance(value, dict):
            account_cls = MT5Account if cls is MT5Config else AccountCredentials
            kwargs[key] = account_cls(**{k: str(v) for k, v in value.items()})
        elif isinstance(value, dict) and isinstance(field_type, str) and field_type in _NESTED:
            kwargs[key] = _coerce(_NESTED[field_type], value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


_NESTED = {
    "BrokerConfig": BrokerConfig,
    "AccountCredentials": AccountCredentials,
    "MT5Config": MT5Config,
    "ManagementConfig": ManagementConfig,
    "ReversalConfig": ReversalConfig,
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
    for name in ("demo", "live"):
        account = getattr(config.broker, name)
        prefix = f"CAPITAL_{name.upper()}_"
        account.api_key = env.get(f"{prefix}API_KEY", account.api_key)
        account.identifier = env.get(f"{prefix}IDENTIFIER", account.identifier)
        account.password = env.get(f"{prefix}PASSWORD", account.password)
        account.account_id = env.get(f"{prefix}ACCOUNT_ID", account.account_id)

    for name in ("demo", "live"):
        login = getattr(config.broker.mt5, name)
        prefix = f"MT5_{name.upper()}_"
        login.login = env.get(f"{prefix}LOGIN", login.login).strip()
        login.password = env.get(f"{prefix}PASSWORD", login.password)
        login.server = env.get(f"{prefix}SERVER", login.server).strip()
        login.terminal_path = env.get(f"{prefix}TERMINAL", login.terminal_path).strip()

    config.news.api_key = env.get("NEWS_API_KEY", config.news.api_key)
    config.llm.api_key = env.get("ANTHROPIC_API_KEY", config.llm.api_key)
    config.telegram.bot_token = env.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token)
    config.telegram.chat_id = env.get("TELEGRAM_CHAT_ID", config.telegram.chat_id)
    if config.telegram.bot_token and config.telegram.chat_id:
        config.telegram.enabled = True
    return config


def resolve_environment_secrets(config: Config) -> Config:
    """Re-read the secrets that depend on which environment was chosen.

    Called after ``--env`` is applied. Running demo and live at once against a
    single Telegram bot token breaks both: two pollers on one token steal each
    other's updates, so commands land in whichever process grabbed them first.
    A per-environment token avoids that entirely.
    """
    import os
    environment = config.broker.environment.upper()
    if environment:
        config.telegram.bot_token = os.environ.get(
            f"TELEGRAM_{environment}_BOT_TOKEN", config.telegram.bot_token
        )
        config.telegram.chat_id = os.environ.get(
            f"TELEGRAM_{environment}_CHAT_ID", config.telegram.chat_id
        )
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
