"""Load and validate the bot's YAML configuration."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_MODES = ("paper", "testnet", "live")


class ConfigError(ValueError):
    pass


@dataclass
class StrategyConfig:
    name: str = "ma_crossover"
    fast: int = 20
    slow: int = 50


@dataclass
class RiskConfig:
    starting_cash: float = 1000.0     # paper mode only
    risk_per_trade: float = 0.01      # fraction of equity lost if the stop-loss is hit
    max_position_pct: float = 0.25    # max fraction of equity in one position
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.06     # 0 disables take-profit
    max_daily_loss_pct: float = 0.03  # no new entries for the rest of the UTC day
    max_drawdown_pct: float = 0.15    # kill switch: close everything and halt


@dataclass
class CostConfig:
    fee_rate: float = 0.001
    slippage: float = 0.0005


@dataclass
class Config:
    mode: str = "paper"
    exchange: str = "binance"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    poll_seconds: int = 60
    state_file: str = "state/bot_state.json"
    log_file: str = "logs/bot.log"
    kill_file: str = "STOP"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ConfigError(f"mode must be one of {VALID_MODES}, got {self.mode!r}")
        if "/" not in self.symbol:
            raise ConfigError(f"symbol must look like BASE/QUOTE, got {self.symbol!r}")
        if self.poll_seconds < 5:
            raise ConfigError("poll_seconds must be at least 5")
        s = self.strategy
        if s.name != "ma_crossover":
            raise ConfigError(f"unknown strategy {s.name!r}")
        if not (0 < s.fast < s.slow):
            raise ConfigError("strategy.fast must be > 0 and smaller than strategy.slow")
        r = self.risk
        for name in ("risk_per_trade", "max_position_pct", "stop_loss_pct",
                     "max_daily_loss_pct", "max_drawdown_pct"):
            value = getattr(r, name)
            if not (0 < value < 1):
                raise ConfigError(f"risk.{name} must be between 0 and 1, got {value}")
        if not (0 <= r.take_profit_pct < 10):
            raise ConfigError("risk.take_profit_pct must be >= 0")
        if r.starting_cash <= 0:
            raise ConfigError("risk.starting_cash must be positive")
        c = self.costs
        if not (0 <= c.fee_rate < 0.1 and 0 <= c.slippage < 0.1):
            raise ConfigError("costs.fee_rate and costs.slippage must be between 0 and 0.1")


def _build(cls, raw: dict, where: str):
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")
    return cls(**raw)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    nested = {
        "strategy": _build(StrategyConfig, raw.pop("strategy", None), "strategy"),
        "risk": _build(RiskConfig, raw.pop("risk", None), "risk"),
        "costs": _build(CostConfig, raw.pop("costs", None), "costs"),
    }
    cfg = _build(Config, raw, "config")
    for key, value in nested.items():
        setattr(cfg, key, value)
    # Relative file paths are resolved next to the config file.
    for key in ("state_file", "log_file", "kill_file"):
        p = Path(getattr(cfg, key))
        if not p.is_absolute():
            setattr(cfg, key, str(path.parent / p))
    cfg.validate()
    return cfg
