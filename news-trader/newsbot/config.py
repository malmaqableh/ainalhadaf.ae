"""Settings, news sources and tradable assets, loaded from YAML and validated."""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_MODES = ("paper", "testnet", "live")
HORIZONS = ("minutes", "hours", "days")


class ConfigError(ValueError):
    pass


# ---- settings.yaml -------------------------------------------------------


@dataclass
class NewsConfig:
    max_age_minutes: int = 30          # older stories are assumed priced in
    dedup_window_hours: int = 6        # how far back to look for the same event
    dedup_similarity: float = 0.6      # headline token overlap that counts as the same event


@dataclass
class AnalysisConfig:
    engine: str = "claude"             # claude | keyword
    model: str = "claude-opus-5-5"
    effort: str = "low"
    max_calls_per_day: int = 500       # hard cap on LLM calls (cost control)
    prefilter: bool = True             # only analyze stories that mention a tracked asset or macro keyword


@dataclass
class ScoringConfig:
    # weighted confidence = model confidence
    #                       x (0.5 + 0.5 x best publisher reliability)
    #                       x confirmation weight for the number of independent sources
    default_reliability: float = 0.5   # publishers not listed in sources.yaml
    # [1 source, 2 sources, 3+ sources]
    confirmation_weights: list[float] = field(default_factory=lambda: [0.85, 0.95, 1.0])


@dataclass
class ConfirmationConfig:
    timeframe: str = "1m"
    momentum_bars: int = 15
    min_momentum_pct: float = 0.002    # price must have moved 0.2% in the signal's direction
    volume_recent_bars: int = 5
    volume_lookback_bars: int = 60
    min_volume_ratio: float = 1.5      # recent volume vs. its usual level
    require_momentum: bool = True
    require_volume: bool = True


@dataclass
class SignalsConfig:
    min_confidence: float = 0.75
    min_independent_sources: int = 2
    entry_limit_offset_pct: float = 0.0005  # limit price this far through the last price
    entry_timeout_seconds: int = 120        # unfilled entry orders are cancelled after this
    confirmation: ConfirmationConfig = field(default_factory=ConfirmationConfig)


@dataclass
class HorizonRisk:
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_minutes: int


def _default_horizons() -> dict[str, HorizonRisk]:
    return {
        "minutes": HorizonRisk(0.01, 0.02, 60),
        "hours": HorizonRisk(0.02, 0.04, 480),
        "days": HorizonRisk(0.04, 0.08, 4320),
    }


@dataclass
class SessionConfig:
    enabled: bool = False              # crypto trades 24/7; enable for stock markets
    timezone: str = "America/New_York"
    open: str = "09:30"
    close: str = "16:00"
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Monday=0
    no_trade_open_minutes: int = 5
    no_trade_close_minutes: int = 5


@dataclass
class RiskConfig:
    starting_cash: float = 10_000.0         # paper account only
    max_risk_per_trade_pct: float = 0.01    # loss at the stop-loss, as a fraction of equity
    max_total_exposure_pct: float = 0.20    # all open + pending positions together
    max_daily_loss_pct: float = 0.03        # flatten everything and stop until the next day
    max_trades_per_day: int = 10
    allow_short: bool = False
    min_order_notional: float = 10.0
    day_timezone: str = "UTC"               # when the trading "day" rolls over
    horizons: dict[str, HorizonRisk] = field(default_factory=_default_horizons)
    session: SessionConfig = field(default_factory=SessionConfig)


@dataclass
class CostConfig:
    fee_rate: float = 0.001
    slippage: float = 0.0005


@dataclass
class AlertsConfig:
    telegram: bool = False
    email: bool = False
    error_cooldown_minutes: int = 15   # don't repeat the same error alert more often than this


@dataclass
class PathsConfig:
    kill_file: str = "data/KILL"
    log_file: str = "logs/newsbot.log"
    paper_state: str = "data/paper_state.json"


@dataclass
class Settings:
    mode: str = "paper"
    exchange: str = "binance"
    quote_currency: str = "USDT"
    database_url: str = "sqlite:///data/newsbot.db"
    tick_seconds: int = 15
    news_poll_seconds: int = 120
    news: NewsConfig = field(default_factory=NewsConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    signals: SignalsConfig = field(default_factory=SignalsConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ConfigError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")
        if not 60 <= self.news_poll_seconds <= 300:
            raise ConfigError("news_poll_seconds must be between 60 and 300")
        if not 1 <= self.tick_seconds <= self.news_poll_seconds:
            raise ConfigError("tick_seconds must be between 1 and news_poll_seconds")
        if self.analysis.engine not in ("claude", "keyword"):
            raise ConfigError("analysis.engine must be 'claude' or 'keyword'")
        if self.analysis.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError("analysis.effort must be low, medium, high, xhigh or max")
        if not 0 < self.news.dedup_similarity <= 1:
            raise ConfigError("news.dedup_similarity must be in (0, 1]")
        w = self.scoring.confirmation_weights
        if not w or any(not 0 < x <= 1 for x in w):
            raise ConfigError("scoring.confirmation_weights must be a list of values in (0, 1]")
        if not 0 < self.signals.min_confidence <= 1:
            raise ConfigError("signals.min_confidence must be in (0, 1]")
        if self.signals.min_independent_sources < 1:
            raise ConfigError("signals.min_independent_sources must be at least 1")
        r = self.risk
        for name in ("max_risk_per_trade_pct", "max_total_exposure_pct", "max_daily_loss_pct"):
            if not 0 < getattr(r, name) < 1:
                raise ConfigError(f"risk.{name} must be between 0 and 1")
        if r.max_trades_per_day < 1:
            raise ConfigError("risk.max_trades_per_day must be at least 1")
        if set(r.horizons) != set(HORIZONS):
            raise ConfigError(f"risk.horizons must define exactly {HORIZONS}")
        for name, h in r.horizons.items():
            if not (0 < h.stop_loss_pct < 0.5 and 0 < h.take_profit_pct < 5 and h.max_hold_minutes > 0):
                raise ConfigError(f"risk.horizons.{name} has invalid values")


# ---- sources.yaml / assets.yaml -----------------------------------------


@dataclass
class SourceConfig:
    name: str
    type: str                      # rss | gdelt | newsapi
    url: str = ""                  # rss
    query: str = ""                # gdelt / newsapi
    publisher: str = ""            # display name; reliability is looked up by article domain
    reliability: float | None = None  # overrides the domain table for every article from this source
    enabled: bool = True
    language: str = "en"


@dataclass
class SourcesConfig:
    sources: list[SourceConfig]
    publisher_reliability: dict[str, float] = field(default_factory=dict)

    def reliability_of(self, domain: str, default: float) -> float:
        return self.publisher_reliability.get(domain, default)


@dataclass
class AssetConfig:
    symbol: str                    # exchange symbol, e.g. BTC/USDT
    asset_class: str = "crypto"
    aliases: list[str] = field(default_factory=list)   # tickers/names an analyzer may return
    keywords: list[str] = field(default_factory=list)  # words that mark a story as relevant


@dataclass
class AssetsConfig:
    assets: list[AssetConfig]
    macro_keywords: list[str] = field(default_factory=list)

    def resolve(self, name: str) -> AssetConfig | None:
        """Map an analyzer's asset name/ticker to a tradable asset."""
        key = name.strip().upper()
        for a in self.assets:
            if key == a.symbol.upper() or key == a.symbol.split("/")[0].upper() or \
                    key in (x.upper() for x in a.aliases):
                return a
        return None


# ---- generic YAML -> dataclass builder ----------------------------------


def _build(tp, raw, where: str):
    origin = typing.get_origin(tp)
    if dataclasses.is_dataclass(tp):
        if raw is None:
            return tp()
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a mapping")
        hints = typing.get_type_hints(tp)
        names = {f.name for f in dataclasses.fields(tp)}
        unknown = set(raw) - names
        if unknown:
            raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")
        kwargs = {k: _build(hints[k], v, f"{where}.{k}") for k, v in raw.items()}
        try:
            return tp(**kwargs)
        except TypeError as e:
            raise ConfigError(f"{where}: {e}") from None
    if origin is list:
        (item,) = typing.get_args(tp)
        if not isinstance(raw, list):
            raise ConfigError(f"{where} must be a list")
        return [_build(item, v, f"{where}[{i}]") for i, v in enumerate(raw)]
    if origin is dict:
        _, val = typing.get_args(tp)
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a mapping")
        return {k: _build(val, v, f"{where}.{k}") for k, v in raw.items()}
    return raw


def _read(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None


def load_settings(path: str | Path) -> Settings:
    path = Path(path).resolve()
    settings = _build(Settings, _read(path), "settings")
    base = path.parent.parent  # config/settings.yaml -> project root
    for key in ("kill_file", "log_file", "paper_state"):
        p = Path(getattr(settings.paths, key))
        if not p.is_absolute():
            setattr(settings.paths, key, str(base / p))
    if settings.database_url.startswith("sqlite:///") and not settings.database_url.startswith("sqlite:////"):
        rel = settings.database_url.removeprefix("sqlite:///")
        if rel != ":memory:":
            settings.database_url = f"sqlite:///{base / rel}"
    settings.validate()
    return settings


def load_sources(path: str | Path) -> SourcesConfig:
    cfg = _build(SourcesConfig, _read(Path(path)), "sources")
    names = [s.name for s in cfg.sources]
    if len(names) != len(set(names)):
        raise ConfigError("source names must be unique")
    for s in cfg.sources:
        if s.type not in ("rss", "gdelt", "newsapi"):
            raise ConfigError(f"source {s.name}: type must be rss, gdelt or newsapi")
        if s.type == "rss" and not s.url:
            raise ConfigError(f"source {s.name}: rss sources need a url")
        if s.type in ("gdelt", "newsapi") and not s.query:
            raise ConfigError(f"source {s.name}: {s.type} sources need a query")
    return cfg


def load_assets(path: str | Path) -> AssetsConfig:
    cfg = _build(AssetsConfig, _read(Path(path)), "assets")
    if not cfg.assets:
        raise ConfigError("assets.yaml must list at least one asset")
    return cfg
