import os
from pathlib import Path

import pytest

from bot.backtest import run_backtest
from bot.broker import LiveTradingNotConfirmed, make_broker
from bot.config import Config, ConfigError, CostConfig, RiskConfig, load_config
from bot.data import fetch_history, load_csv, save_csv
from bot.risk import RiskManager
from bot.state import BotState
from bot.strategy import MACrossover
from conftest import HOUR_MS, FakeExchange, make_candles

ROOT = Path(__file__).resolve().parents[1]
NO_COSTS = CostConfig(fee_rate=0, slippage=0)


def test_backtest_enters_next_bar_open_no_lookahead():
    closes = [10, 10, 10, 10, 10, 12, 13, 14, 15, 16]
    candles = make_candles(closes)
    risk = RiskManager(RiskConfig(take_profit_pct=0, max_position_pct=0.5))
    result = run_backtest(candles, MACrossover(2, 4), risk, NO_COSTS, 1000)
    trade = result.trades[0]
    assert trade["opened"] == candles[6][0]          # signal on bar 5, filled on bar 6
    assert trade["entry_price"] == candles[6][1]     # at bar 6's open
    assert trade["reason"] == "end of data"
    assert result.end_equity > 1000


def test_backtest_stop_loss_and_costs():
    closes = [10, 10, 10, 10, 10, 12, 12, 10, 10, 10]
    candles = make_candles(closes)
    risk = RiskManager(RiskConfig(stop_loss_pct=0.03, take_profit_pct=0))
    costs = CostConfig(fee_rate=0.001, slippage=0.001)
    result = run_backtest(candles, MACrossover(2, 4), risk, costs, 1000)
    assert result.trades[0]["reason"] == "stop-loss"
    assert result.trades[0]["pnl"] < 0
    assert result.fees_paid > 0
    assert 0 < result.max_drawdown < 0.05
    assert "Total return" in result.report()


def test_backtest_needs_enough_data():
    with pytest.raises(ValueError):
        run_backtest(make_candles([1, 2, 3]), MACrossover(2, 4), RiskManager(RiskConfig()), NO_COSTS, 1000)


def test_fetch_history_paginates_and_drops_forming_candle():
    candles = make_candles(list(range(1, 26)))
    ex = FakeExchange(candles)
    until = candles[-1][0] + HOUR_MS // 2  # last candle still forming
    got = fetch_history(ex, "BTC/USDT", "1h", candles[0][0], until_ms=until, page_limit=7)
    assert got == candles[:-1]


def test_csv_roundtrip(tmp_path):
    candles = make_candles([1.5, 2.5, 3.5])
    save_csv(candles, tmp_path / "c.csv")
    assert load_csv(tmp_path / "c.csv") == candles


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.mode == "paper"
    assert Path(cfg.state_file).is_absolute()


def test_config_rejects_typos(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("mode: paper\nrisk:\n  stop_los_pct: 0.1\n")
    with pytest.raises(ConfigError, match="stop_los_pct"):
        load_config(p)
    p.write_text("mode: yolo\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_live_mode_requires_explicit_confirmation(monkeypatch):
    monkeypatch.delenv("BOT_CONFIRM_LIVE", raising=False)
    monkeypatch.setenv("BOT_API_KEY", "k")
    monkeypatch.setenv("BOT_API_SECRET", "s")
    with pytest.raises(LiveTradingNotConfirmed, match="BOT_CONFIRM_LIVE"):
        make_broker(Config(mode="live"), BotState())


def test_testnet_requires_keys(monkeypatch):
    monkeypatch.delenv("BOT_API_KEY", raising=False)
    monkeypatch.delenv("BOT_API_SECRET", raising=False)
    with pytest.raises(LiveTradingNotConfirmed, match="BOT_API_KEY"):
        make_broker(Config(mode="testnet"), BotState())
