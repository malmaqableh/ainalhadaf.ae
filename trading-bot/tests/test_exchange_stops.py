import json
from datetime import datetime, timezone

import ccxt
import pytest

from bot.broker import LiveBroker
from bot.config import Config, CostConfig, RiskConfig, StrategyConfig
from bot.engine import Engine
from bot.risk import RiskManager
from bot.state import BotState, StateStore
from bot.strategy import make_strategy
from conftest import HOUR_MS, FakeLiveExchange, make_candles

CROSS_UP = [10, 10, 10, 10, 10, 12]  # BUY on the last closed bar with fast=2, slow=4


def build(tmp_path, stop_order_type="limit", **risk_kw):
    cfg = Config(
        mode="live", timeframe="1h",
        state_file=str(tmp_path / "state.json"), log_file=str(tmp_path / "bot.log"),
        kill_file=str(tmp_path / "STOP"),
        strategy=StrategyConfig(fast=2, slow=4),
        risk=RiskConfig(stop_order_type=stop_order_type, **risk_kw),
        costs=CostConfig(fee_rate=0.001, slippage=0))
    candles = make_candles(CROSS_UP)
    exchange = FakeLiveExchange(candles, price=12)
    now = datetime.fromtimestamp((candles[-1][0] + HOUR_MS + 60_000) / 1000, timezone.utc)
    state = BotState()
    store = StateStore(cfg.state_file)
    broker = LiveBroker(exchange, cfg.symbol, cfg.timeframe,
                        cfg.risk.stop_order_type, cfg.risk.stop_limit_offset_pct)
    engine = Engine(cfg, broker, make_strategy(cfg.strategy), RiskManager(cfg.risk), store, state,
                    clock=lambda: now, sleep=lambda s: None)
    return engine, exchange, state, store


def kinds(exchange):
    return [c[0] for c in exchange.calls]


def test_buy_places_stop_limit_on_exchange(tmp_path):
    engine, ex, state, store = build(tmp_path)
    engine.step()
    pos = state.position
    assert kinds(ex) == ["market", "stop"]
    _, order_type, amount, limit, trigger = ex.calls[1]
    assert order_type == "limit"
    assert amount == pytest.approx(pos.qty, abs=1e-6)
    assert trigger == pytest.approx(11.64)              # 3% below the 12.00 entry
    assert limit == pytest.approx(11.52)                # 1% below the trigger, rounded to tick
    assert pos.stop_order_id == "2"
    assert store.load().position.stop_order_id == "2"   # survives a restart


def test_stop_market_type(tmp_path):
    engine, ex, state, _ = build(tmp_path, stop_order_type="market")
    engine.step()
    assert ex.calls[1][1:4:2] == ("market", None)       # no limit price


def test_exchange_stop_fill_is_booked_without_selling_again(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    engine.step()
    ex.fill_stop(state.position.stop_order_id)
    ex.price = 11.4
    engine.step()
    assert state.position is None
    assert state.trades[-1]["reason"] == "stop-loss (exchange)"
    assert state.trades[-1]["exit_price"] == pytest.approx(11.52)
    assert kinds(ex) == ["market", "stop"]              # no extra market sell


def test_stop_filled_while_bot_was_offline(tmp_path):
    engine, ex, state, store = build(tmp_path)
    engine.step()
    ex.fill_stop(state.position.stop_order_id)
    restarted = Engine(engine.cfg, engine.broker, engine.strategy, engine.risk, store, store.load(),
                       clock=engine.clock, sleep=engine.sleep)
    restarted.step()
    assert restarted.state.position is None
    assert restarted.state.trades[-1]["reason"] == "stop-loss (exchange)"


def test_bot_exit_cancels_stop_before_selling(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    engine.step()
    engine._exit(12.5, "sell signal")
    assert kinds(ex) == ["market", "stop", "cancel", "market"]
    assert state.position is None
    assert state.trades[-1]["reason"] == "sell signal"
    assert ex.balance["BTC"] == pytest.approx(0, abs=1e-6)


def test_stop_that_filled_just_before_cancel_is_not_sold_twice(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    engine.step()
    ex.fill_stop(state.position.stop_order_id)
    engine._exit(11.0, "sell signal")
    assert kinds(ex) == ["market", "stop"]              # cancel failed (already filled), no sell
    assert [t["reason"] for t in state.trades] == ["stop-loss (exchange)"]
    assert state.position is None


def test_partial_stop_fill_then_bot_sells_the_rest(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    engine.step()
    total = state.position.qty
    ex.fill_stop(state.position.stop_order_id, qty=5.0)
    engine._exit(11.0, "stop-loss")
    assert [t["reason"] for t in state.trades] == ["stop-loss (exchange)", "stop-loss"]
    assert state.trades[0]["qty"] == pytest.approx(5.0)
    assert state.trades[1]["qty"] == pytest.approx(total - 5.0, abs=1e-6)
    assert state.position is None


def test_stop_cancelled_outside_bot_is_replaced(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    engine.step()
    ex.cancel_order(state.position.stop_order_id, "BTC/USDT")   # e.g. someone cancels it in the app
    engine.step()
    assert state.position.stop_order_id == "3"
    assert ex.orders["3"]["status"] == "open"


def test_exchange_without_stop_orders_falls_back_to_bot_watching(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    ex.stop_error = ccxt.NotSupported("no stops here")
    engine.step()
    assert state.position.stop_order_id is None
    assert engine.exchange_stops is False
    ex.price = 11.5                                    # below the 11.64 stop
    engine.step()
    assert state.trades[-1]["reason"] == "stop-loss"
    assert state.position is None


def test_failed_stop_placement_is_retried(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    ex.stop_error = ccxt.NetworkError("timeout")
    engine.step()
    assert state.position.stop_order_id is None
    ex.stop_error = None
    engine.step()
    assert state.position.stop_order_id is not None


def test_no_stop_placed_when_price_already_below_it(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    ex.stop_error = ccxt.NetworkError("timeout")
    engine.step()
    ex.stop_error = None
    ex.price = 11.6                                    # below the stop: bot sells itself
    engine.step()
    assert "stop" not in kinds(ex)
    assert state.trades[-1]["reason"] == "stop-loss"


def test_exchange_stops_can_be_turned_off(tmp_path):
    engine, ex, state, _ = build(tmp_path, exchange_stop_loss=False)
    engine.step()
    assert kinds(ex) == ["market"]


def test_kill_file_leaves_exchange_stop_in_place(tmp_path):
    engine, ex, state, _ = build(tmp_path)
    engine.step()
    (tmp_path / "STOP").write_text("")
    assert engine.step() is False
    assert ex.orders[state.position.stop_order_id]["status"] == "open"


def test_old_state_file_without_stop_id_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"initialized": True, "position": {
        "qty": 1.0, "entry_price": 10.0, "entry_fee": 0.01, "stop_price": 9.7,
        "take_profit_price": None, "opened_at": "2026-01-01T00:00:00+00:00"}}))
    assert StateStore(path).load().position.stop_order_id is None
