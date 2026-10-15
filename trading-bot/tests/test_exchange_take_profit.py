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

CROSS_UP = [10, 10, 10, 10, 10, 12]  # BUY at 12.00 with fast=2, slow=4: stop 11.64, take-profit 12.72


def build(tmp_path, exchange_id="binance", stop_order_type="limit", **risk_kw):
    cfg = Config(
        mode="live", exchange=exchange_id, timeframe="1h",
        state_file=str(tmp_path / "state.json"), log_file=str(tmp_path / "bot.log"),
        kill_file=str(tmp_path / "STOP"),
        strategy=StrategyConfig(fast=2, slow=4),
        risk=RiskConfig(stop_order_type=stop_order_type, **risk_kw),
        costs=CostConfig(fee_rate=0.001, slippage=0))
    candles = make_candles(CROSS_UP)
    exchange = FakeLiveExchange(candles, price=12, exchange_id=exchange_id)
    now = datetime.fromtimestamp((candles[-1][0] + HOUR_MS + 60_000) / 1000, timezone.utc)
    state = BotState()
    broker = LiveBroker(exchange, cfg.symbol, cfg.timeframe,
                        cfg.risk.stop_order_type, cfg.risk.stop_limit_offset_pct)
    engine = Engine(cfg, broker, make_strategy(cfg.strategy), RiskManager(cfg.risk),
                    StateStore(cfg.state_file), state, clock=lambda: now, sleep=lambda s: None)
    return engine, exchange, state


def kinds(exchange):
    return [c[0] for c in exchange.calls]


def test_buy_places_oco_with_stop_and_take_profit(tmp_path):
    engine, ex, state = build(tmp_path)
    engine.step()
    assert kinds(ex) == ["market", "oco"]
    request = ex.calls[1][1]
    assert request == {
        "symbol": "BTCUSDT", "side": "SELL", "quantity": ex.amount_to_precision("", state.position.qty),
        "aboveType": "LIMIT_MAKER", "abovePrice": "12.72",
        "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": "11.64",
        "belowPrice": "11.52", "belowTimeInForce": "GTC"}
    assert ex.orders[state.position.stop_order_id]["type"] == "STOP_LOSS_LIMIT"
    assert ex.orders[state.position.tp_order_id]["type"] == "LIMIT_MAKER"
    assert ex.fetch_balance()["free"]["BTC"] == pytest.approx(0, abs=1e-6)  # coins locked once


def test_oco_with_stop_market_leg(tmp_path):
    engine, ex, _ = build(tmp_path, stop_order_type="market")
    engine.step()
    request = ex.calls[1][1]
    assert request["belowType"] == "STOP_LOSS"
    assert "belowPrice" not in request and "belowTimeInForce" not in request


def test_take_profit_filled_on_exchange(tmp_path):
    engine, ex, state = build(tmp_path)
    engine.step()
    ex.fill_order(state.position.tp_order_id)
    ex.price = 12.8
    engine.step()
    assert state.position is None
    assert [t["reason"] for t in state.trades] == ["take-profit (exchange)"]
    assert state.trades[0]["exit_price"] == pytest.approx(12.72)
    assert state.trades[0]["pnl"] > 0
    assert kinds(ex) == ["market", "oco"]          # no extra sell


def test_stop_leg_filled_on_exchange(tmp_path):
    engine, ex, state = build(tmp_path)
    engine.step()
    ex.fill_order(state.position.stop_order_id)
    ex.price = 11.4
    engine.step()
    assert [t["reason"] for t in state.trades] == ["stop-loss (exchange)"]
    assert state.position is None


def test_bot_exit_cancels_oco_then_sells(tmp_path):
    engine, ex, state = build(tmp_path)
    engine.step()
    engine._exit(12.3, "sell signal")
    assert kinds(ex) == ["market", "oco", "cancel", "market"]  # one cancel removes both legs
    assert state.trades[-1]["reason"] == "sell signal"
    assert state.position is None
    assert ex.balance["BTC"] == pytest.approx(0, abs=1e-6)


def test_partial_take_profit_then_oco_replaced_for_the_rest(tmp_path):
    engine, ex, state = build(tmp_path)
    engine.step()
    total = state.position.qty
    tp_id = state.position.tp_order_id
    ex.fill_order(tp_id, qty=5.0)                  # partial fill expires the stop leg
    engine.step()
    assert state.trades[-1]["reason"] == "take-profit (exchange)"
    assert state.trades[-1]["qty"] == pytest.approx(5.0)
    assert state.position.qty == pytest.approx(total - 5.0)
    assert ex.orders[tp_id]["status"] == "canceled"  # rest of the old leg cancelled
    assert kinds(ex)[-1] == "oco"                  # new OCO protects the remaining coins
    assert float(ex.calls[-1][1]["quantity"]) == pytest.approx(total - 5.0, abs=1e-6)


def test_take_profit_on_exchange_can_be_turned_off(tmp_path):
    engine, ex, state = build(tmp_path, exchange_take_profit=False)
    engine.step()
    assert kinds(ex) == ["market", "stop"]
    assert state.position.tp_order_id is None


def test_no_take_profit_configured_places_stop_only(tmp_path):
    engine, ex, _ = build(tmp_path, take_profit_pct=0)
    engine.step()
    assert kinds(ex) == ["market", "stop"]


def test_other_exchanges_place_stop_only(tmp_path):
    engine, ex, state = build(tmp_path, exchange_id="bybit")
    engine.step()
    assert kinds(ex) == ["market", "stop"]
    assert engine.broker.supports_oco is False


def test_oco_rejected_as_unsupported_falls_back_to_stop(tmp_path):
    engine, ex, state = build(tmp_path)
    ex.oco_error = ccxt.NotSupported("no OCO")
    engine.step()
    assert kinds(ex) == ["market", "stop"]
    assert engine.exchange_take_profits is False
    assert state.position.stop_order_id and not state.position.tp_order_id


def test_unreadable_oco_response_cancels_the_orders(tmp_path):
    engine, ex, state = build(tmp_path)
    ex.oco_response_override = lambda r: {"orderListId": r["orderListId"], "orders": r["orders"]}
    engine.step()
    assert state.position.stop_order_id is None
    assert all(o["status"] == "canceled" for o in ex.orders.values())  # nothing left untracked
    assert ex.fetch_balance()["free"]["BTC"] == pytest.approx(state.position.qty, abs=1e-6)
