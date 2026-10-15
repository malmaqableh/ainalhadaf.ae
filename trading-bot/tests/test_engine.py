from datetime import datetime, timezone

import pytest

from bot.broker import PaperBroker
from bot.config import Config, CostConfig, RiskConfig, StrategyConfig
from bot.engine import Engine
from bot.risk import RiskManager
from bot.state import BotState, StateStore
from bot.strategy import make_strategy
from conftest import HOUR_MS, FakeExchange, make_candles

CROSS_UP = [10, 10, 10, 10, 10, 12]  # BUY on the last closed bar with fast=2, slow=4


def build(tmp_path, closes=CROSS_UP, price=None, **risk_kw):
    cfg = Config(
        timeframe="1h", poll_seconds=60,
        state_file=str(tmp_path / "state.json"), log_file=str(tmp_path / "bot.log"),
        kill_file=str(tmp_path / "STOP"),
        strategy=StrategyConfig(fast=2, slow=4),
        risk=RiskConfig(starting_cash=1000, **risk_kw),
        costs=CostConfig(fee_rate=0.001, slippage=0))
    candles = make_candles(closes)
    # Exchange also returns a still-forming candle, which the engine must ignore.
    forming = [candles[-1][0] + HOUR_MS, 12, 12, 12, 12, 0]
    exchange = FakeExchange(candles + [forming], price=price or closes[-1])
    now = datetime.fromtimestamp((forming[0] + 60_000) / 1000, timezone.utc)
    state = BotState(cash=cfg.risk.starting_cash)
    store = StateStore(cfg.state_file)
    broker = PaperBroker(exchange, cfg.symbol, cfg.timeframe, cfg.costs, state)
    engine = Engine(cfg, broker, make_strategy(cfg.strategy), RiskManager(cfg.risk), store, state,
                    clock=lambda: now, sleep=lambda s: None)
    return engine, exchange, state, store


def test_buys_on_crossover_and_saves_state(tmp_path):
    engine, _, state, store = build(tmp_path)
    assert engine.step() is True
    pos = state.position
    assert pos is not None
    # capped by max_position_pct: 25% of 1000 at price 12
    assert pos.qty == pytest.approx(250 / 12)
    assert pos.stop_price == pytest.approx(12 * 0.97)
    assert state.cash == pytest.approx(1000 - 250 - 0.25)
    assert store.load().position == pos


def test_forming_candle_is_ignored_and_candle_traded_once(tmp_path):
    engine, _, state, _ = build(tmp_path)
    engine.step()
    assert state.last_candle_ts == make_candles(CROSS_UP)[-1][0]
    state.position = None  # pretend it was closed; the same candle must not re-enter
    engine.step()
    assert state.position is None


def test_stop_loss_closes_position(tmp_path):
    engine, exchange, state, _ = build(tmp_path)
    engine.step()
    exchange.price = 11.5  # below the 3% stop at 11.64
    engine.step()
    assert state.position is None
    assert state.trades[-1]["reason"] == "stop-loss"
    assert state.trades[-1]["pnl"] < 0


def test_take_profit_closes_position(tmp_path):
    engine, exchange, state, _ = build(tmp_path)
    engine.step()
    exchange.price = 13  # above 6% take-profit at 12.72
    engine.step()
    assert state.trades[-1]["reason"] == "take-profit"
    assert state.trades[-1]["pnl"] > 0


def test_drawdown_kill_switch_sells_and_halts(tmp_path):
    engine, exchange, state, store = build(tmp_path, max_drawdown_pct=0.04)
    engine.step()
    exchange.price = 9.6  # 20% drop on a 25% position = 5% of equity
    assert engine.step() is False
    assert state.position is None
    assert state.halted
    assert state.trades[-1]["reason"] == "drawdown kill switch"
    assert store.load().halted
    assert engine.step() is False  # stays halted until a human resumes


def test_daily_loss_limit_blocks_new_entries(tmp_path):
    engine, _, state, _ = build(tmp_path)
    state.day = engine.clock().date().isoformat()
    state.day_start_equity = 2000  # already down 50% today
    engine.step()
    assert state.position is None


def test_kill_file_stops_bot(tmp_path):
    engine, _, state, _ = build(tmp_path)
    (tmp_path / "STOP").write_text("")
    assert engine.step() is False
    assert state.position is None


def test_run_keeps_going_after_errors(tmp_path):
    engine, exchange, state, _ = build(tmp_path)
    calls = {"n": 0}
    real = exchange.fetch_ticker

    def flaky(symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("network blip")
        if calls["n"] == 3:
            (tmp_path / "STOP").write_text("")
        return real(symbol)

    exchange.fetch_ticker = flaky
    engine.run()
    assert calls["n"] == 3
    assert state.position is not None  # traded after recovering from the error
