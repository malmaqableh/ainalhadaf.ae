from datetime import timedelta

import pytest

from newsbot.config import CostConfig, RiskConfig, Settings
from newsbot.execution.ccxt_broker import CcxtSpotBroker
from newsbot.execution.executor import Executor
from newsbot.execution.factory import LiveTradingLocked, make_broker
from newsbot.execution.sim import SimulatedBroker
from newsbot.risk.manager import AccountSnapshot, OrderIntent, RiskManager
from newsbot.storage.db import Bracket
from conftest import T0, Clock
from fakes import FakeExchange, FakeFeed

COSTS = CostConfig(fee_rate=0.001, slippage=0.001)


def setup(db, tmp_path, broker=None, clock=None, feed=None, **risk_kw):
    clock = clock or Clock()
    feed = feed or FakeFeed()
    broker = broker or SimulatedBroker(feed, COSTS, 10_000, state_path=str(tmp_path / "paper.json"), clock=clock)
    risk = RiskManager(RiskConfig(max_total_exposure_pct=0.5, **risk_kw), COSTS, db,
                       kill_file=str(tmp_path / "KILL"), entry_offset_pct=0, clock=clock,
                       broker_supports_short=broker.supports_short)
    risk.update(broker.account())
    notes = []
    ex = Executor(db, broker, risk, notify=lambda k, m: notes.append((k, m)), clock=clock)
    return ex, risk, broker, feed, clock, notes


def approve(risk, broker, symbol="BTC/USDT", side="long", horizon="hours"):
    order, reason = risk.approve(OrderIntent(symbol, side, horizon, 0.9, "news"), broker.account(),
                                 [], broker.last_price(symbol))
    assert order, reason
    return order


# ---- simulated broker fill rules ------------------------------------------------

def test_limit_entry_fills_then_take_profit(db, tmp_path):
    ex, risk, broker, feed, clock, notes = setup(db, tmp_path)
    b = ex.open(approve(risk, broker))
    assert b.status == "pending"
    ex.manage()                                     # price still at the limit: fills
    b = db.active_brackets()[0]
    assert b.status == "open" and b.entry_price == pytest.approx(100)
    feed.move("BTC/USDT", 100, 104.5, 99.5, 104.2)  # through the 104 target
    ex.manage()
    b = db.session().get(Bracket, b.id)
    assert b.status == "closed" and b.exit_reason == "take-profit"
    assert b.exit_price == pytest.approx(104)       # limit order: no slippage
    assert b.pnl == pytest.approx(b.qty * 4 - b.entry_fee - b.exit_fee)
    assert [k for k, _ in notes] == ["trade", "trade"]


def test_stop_loss_with_gap_and_slippage(db, tmp_path):
    ex, risk, broker, feed, clock, _ = setup(db, tmp_path)
    ex.open(approve(risk, broker))
    ex.manage()
    feed.move("BTC/USDT", 97, 97.5, 96, 96.5)       # gapped below the 98 stop
    ex.manage()
    b = db.session().get(Bracket, 1)
    assert b.exit_reason == "stop-loss"
    assert b.exit_price == pytest.approx(97 * 0.999)  # fills at the worse open, minus slippage


def test_stop_assumed_first_when_both_touched(db, tmp_path):
    ex, risk, broker, feed, clock, _ = setup(db, tmp_path)
    ex.open(approve(risk, broker))
    ex.manage()
    feed.move("BTC/USDT", 100, 105, 97, 101)
    ex.manage()
    assert db.session().get(Bracket, 1).exit_reason == "stop-loss"


def test_limit_not_reached_then_cancelled_at_deadline(db, tmp_path):
    ex, risk, broker, feed, clock, _ = setup(db, tmp_path)
    feed.prices["BTC/USDT"] = 100
    order = approve(risk, broker)
    feed.prices["BTC/USDT"] = 101                   # moved away before the order could fill
    ex.open(order)
    ex.manage()
    assert db.active_brackets()[0].status == "pending"
    clock.t = T0 + timedelta(minutes=3)
    ex.manage()
    b = db.session().get(Bracket, 1)
    assert b.status == "canceled" and "not filled" in b.exit_reason
    assert broker.cash == pytest.approx(10_000)


def test_max_hold_time_closes_position(db, tmp_path):
    ex, risk, broker, feed, clock, _ = setup(db, tmp_path)
    ex.open(approve(risk, broker, horizon="minutes"))   # 60 minute max hold
    ex.manage()
    clock.t = T0 + timedelta(minutes=61)
    ex.manage()
    assert db.session().get(Bracket, 1).exit_reason == "max holding time reached"


def test_paper_account_survives_restart(db, tmp_path):
    ex, risk, broker, feed, clock, _ = setup(db, tmp_path)
    ex.open(approve(risk, broker))
    ex.manage()
    again = SimulatedBroker(feed, COSTS, 10_000, state_path=str(tmp_path / "paper.json"), clock=clock)
    assert again.cash == pytest.approx(broker.cash)
    assert again.account().equity == pytest.approx(broker.account().equity)


def test_short_position_mirrors_long(db, tmp_path):
    feed = FakeFeed()
    clock = Clock()
    broker = SimulatedBroker(feed, COSTS, 10_000, supports_short=True, clock=clock)
    ex, risk, *_ = setup(db, tmp_path, broker=broker, clock=clock, feed=feed, allow_short=True)
    ex.open(approve(risk, broker, side="short"))
    ex.manage()
    feed.move("BTC/USDT", 100, 100.5, 95.5, 95.8)   # target for a short is 96
    ex.manage()
    b = db.session().get(Bracket, 1)
    assert b.exit_reason == "take-profit" and b.pnl > 0


# ---- executor safety -------------------------------------------------------------

def test_executor_only_accepts_approved_orders(db, tmp_path):
    ex, *_ = setup(db, tmp_path)
    with pytest.raises(TypeError):
        ex.open(OrderIntent("BTC/USDT", "long", "hours", 0.99, "skip risk"))


def test_executor_refuses_reused_approval(db, tmp_path):
    ex, risk, broker, *_ = setup(db, tmp_path)
    order = approve(risk, broker)
    assert ex.open(order) is not None
    assert ex.open(order) is None


def test_flatten_all_cancels_pending_and_closes_open(db, tmp_path):
    ex, risk, broker, feed, clock, _ = setup(db, tmp_path)
    ex.open(approve(risk, broker, "BTC/USDT"))
    ex.manage()                                     # BTC open
    feed.prices["ETH/USDT"] = 100
    o = approve(risk, broker, "ETH/USDT")
    feed.prices["ETH/USDT"] = 101
    ex.open(o)                                      # ETH pending
    assert ex.flatten_all("kill switch", ["BTC/USDT", "ETH/USDT"]) == 0
    with db.session() as s:
        rows = {b.symbol: b for b in s.query(Bracket)}
    assert rows["BTC/USDT"].status == "closed" and rows["BTC/USDT"].exit_reason == "kill switch"
    assert rows["ETH/USDT"].status == "canceled"
    assert not broker.positions


def test_broker_error_on_one_bracket_does_not_stop_others(db, tmp_path):
    ex, risk, broker, feed, clock, notes = setup(db, tmp_path)
    ex.open(approve(risk, broker, "BTC/USDT"))
    ex.open(approve(risk, broker, "ETH/USDT"))
    real = broker.refresh

    def flaky(b):
        if b.symbol == "BTC/USDT":
            raise ConnectionError("exchange timeout")
        real(b)
    broker.refresh = flaky
    ex.manage()
    states = {b.symbol: b.status for b in db.active_brackets()}
    assert states == {"BTC/USDT": "pending", "ETH/USDT": "open"}
    assert notes[0][0] == "error"


# ---- exchange (ccxt) spot broker -------------------------------------------------------

def spot(db, tmp_path, exchange_id="binance"):
    fx = FakeExchange(exchange_id)
    clock = Clock()
    broker = CcxtSpotBroker(fx, "USDT", ["BTC/USDT"], clock=clock)
    ex, risk, *_ = setup(db, tmp_path, broker=broker, clock=clock)
    return ex, risk, broker, fx, clock


def test_spot_limit_entry_then_oco_protection(db, tmp_path):
    ex, risk, broker, fx, clock = spot(db, tmp_path)
    b = ex.open(approve(risk, broker))
    assert fx.calls[0] == ("limit", "buy", b.qty, 100.0)
    fx.fill(b.entry_order_id)
    ex.manage()
    b = db.active_brackets()[0]
    assert b.status == "open" and b.stop_order_id and b.tp_order_id
    oco = fx.calls[-1][1]
    assert oco["aboveType"] == "LIMIT_MAKER" and oco["abovePrice"] == "104.00"
    assert oco["belowStopPrice"] == "98.00" and oco["belowPrice"] == "97.51"


def test_spot_take_profit_fill_closes_bracket(db, tmp_path):
    ex, risk, broker, fx, clock = spot(db, tmp_path)
    b = ex.open(approve(risk, broker))
    fx.fill(b.entry_order_id)
    ex.manage()
    b = db.active_brackets()[0]
    fx.fill(b.tp_order_id)
    ex.manage()
    b = db.session().get(Bracket, b.id)
    assert b.status == "closed" and b.exit_reason == "take-profit (exchange)" and b.pnl > 0
    assert not fx.open_orders()


def test_spot_exit_cancels_oco_before_market_sell(db, tmp_path):
    ex, risk, broker, fx, clock = spot(db, tmp_path)
    b = ex.open(approve(risk, broker))
    fx.fill(b.entry_order_id)
    ex.manage()
    assert ex.close_symbol("BTC/USDT", "bearish news")
    kinds = [c[0] for c in fx.calls]
    assert kinds[-2:] == ["cancel", "market"]
    assert fx.balance["BTC"] == pytest.approx(0, abs=1e-6)
    assert db.session().get(Bracket, b.id).exit_reason == "bearish news"


def test_spot_backup_stop_when_price_skips_stop_limit(db, tmp_path):
    ex, risk, broker, fx, clock = spot(db, tmp_path)
    b = ex.open(approve(risk, broker))
    fx.fill(b.entry_order_id)
    ex.manage()
    fx.price = 96                                    # below the stop-limit's 97.51 floor; it never filled
    broker.feed._cache.clear()
    ex.manage()
    b = db.session().get(Bracket, b.id)
    assert b.status == "closed" and b.exit_reason == "stop-loss"


def test_spot_partial_entry_kept_and_protected_on_timeout(db, tmp_path):
    ex, risk, broker, fx, clock = spot(db, tmp_path)
    b = ex.open(approve(risk, broker))
    fx.fill(b.entry_order_id, qty=b.qty / 2)
    clock.t = T0 + timedelta(minutes=3)
    ex.manage()
    b = db.active_brackets()[0]
    assert b.status == "open" and b.qty == pytest.approx(fx.orders["1"]["filled"])
    assert b.stop_order_id


def test_spot_oco_cancelled_outside_bot_is_replaced(db, tmp_path):
    ex, risk, broker, fx, clock = spot(db, tmp_path)
    b = ex.open(approve(risk, broker))
    fx.fill(b.entry_order_id)
    ex.manage()
    old = db.active_brackets()[0].stop_order_id
    fx.cancel_order(old, "BTC/USDT")
    ex.manage()
    b = db.active_brackets()[0]
    assert b.status == "open" and b.stop_order_id and b.stop_order_id != old


def test_spot_non_binance_uses_stop_order_only(db, tmp_path):
    ex, risk, broker, fx, clock = spot(db, tmp_path, exchange_id="kraken")
    b = ex.open(approve(risk, broker))
    fx.fill(b.entry_order_id)
    ex.manage()
    assert fx.calls[-1][0] == "stop"
    assert db.active_brackets()[0].tp_order_id == ""


def test_spot_broker_is_long_only(db, tmp_path):
    ex, risk, broker, fx, clock = spot(db, tmp_path)
    order, reason = risk.approve(OrderIntent("BTC/USDT", "short", "hours", 0.9, "x"),
                                 broker.account(), [], 100)
    assert order is None and "short" in reason


# ---- live-mode lock ----------------------------------------------------------------------

def test_live_mode_needs_explicit_confirmation(monkeypatch, assets):
    monkeypatch.setenv("EXCHANGE_API_KEY", "k")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s")
    monkeypatch.delenv("NEWSBOT_CONFIRM_LIVE", raising=False)
    with pytest.raises(LiveTradingLocked, match="NEWSBOT_CONFIRM_LIVE"):
        make_broker(Settings(mode="live"), assets)


def test_testnet_needs_keys(monkeypatch, assets):
    monkeypatch.delenv("EXCHANGE_API_KEY", raising=False)
    with pytest.raises(LiveTradingLocked, match="EXCHANGE_API_KEY"):
        make_broker(Settings(mode="testnet"), assets)


def test_paper_mode_is_simulated_without_keys(monkeypatch, assets, tmp_path):
    monkeypatch.delenv("EXCHANGE_API_KEY", raising=False)
    s = Settings()
    s.paths.paper_state = str(tmp_path / "p.json")
    assert isinstance(make_broker(s, assets), SimulatedBroker)
