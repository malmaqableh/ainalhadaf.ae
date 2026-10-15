"""Risk management: every limit, and attempts to get around them."""

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from newsbot.config import CostConfig, RiskConfig, SessionConfig
from newsbot.risk.manager import AccountSnapshot, ApprovedOrder, OrderIntent, RiskAction, RiskManager
from newsbot.risk.session import session_block_reason
from newsbot.storage.db import Bracket
from conftest import T0, Clock

ACCOUNT = AccountSnapshot(equity=10_000, cash=10_000)
NO_COSTS = CostConfig(fee_rate=0, slippage=0)


def intent(symbol="BTC/USDT", side="long", horizon="hours", **kw):
    return OrderIntent(symbol=symbol, side=side, horizon=horizon, confidence=0.9, reason="test", **kw)


def make(db, tmp_path, clock=None, costs=NO_COSTS, supports_short=False, **risk_kw):
    rm = RiskManager(RiskConfig(**risk_kw), costs, db, kill_file=str(tmp_path / "KILL"),
                     entry_offset_pct=0, broker_supports_short=supports_short, clock=clock or Clock())
    rm.update(ACCOUNT)
    return rm


def bracket(symbol="ETH/USDT", qty=10.0, price=100.0, status="open"):
    return Bracket(symbol=symbol, side="long", status=status, qty=qty, limit_price=price, entry_price=price,
                   stop_price=price * 0.98, take_profit_price=price * 1.04, entry_deadline=T0)


# ---- position sizing ------------------------------------------------------

def test_risk_per_trade_is_one_percent_of_equity(db, tmp_path):
    rm = make(db, tmp_path)
    order, reason = rm.approve(intent(), ACCOUNT, [], last_price=100)
    # hours horizon: 2% stop. 1% of 10,000 = 100 at risk -> 100 / 2 per unit = 50 units ...
    # ... but 50 units = 5,000 notional > 20% exposure cap (2,000), so the cap wins: 20 units.
    assert order.qty == pytest.approx(20)
    assert order.stop_price == pytest.approx(98)
    assert order.take_profit_price == pytest.approx(104)


def test_risk_limit_binds_with_wide_stop(db, tmp_path):
    rm = make(db, tmp_path, max_total_exposure_pct=0.9)
    order, _ = rm.approve(intent(horizon="days"), ACCOUNT, [], last_price=100)   # 4% stop
    loss_at_stop = order.qty * (order.limit_price - order.stop_price)
    assert loss_at_stop == pytest.approx(100)                                   # exactly 1% of equity


def test_fees_and_slippage_are_included_in_risk(db, tmp_path):
    rm = make(db, tmp_path, costs=CostConfig(fee_rate=0.001, slippage=0.001), max_total_exposure_pct=0.9)
    order, _ = rm.approve(intent(horizon="days"), ACCOUNT, [], last_price=100)
    worst_loss = order.qty * ((order.limit_price - order.stop_price) + order.limit_price * 0.001
                              + order.stop_price * 0.002)
    assert worst_loss == pytest.approx(100)


def test_risk_pct_is_configurable(db, tmp_path):
    rm = make(db, tmp_path, max_risk_per_trade_pct=0.005, max_total_exposure_pct=0.9)
    order, _ = rm.approve(intent(horizon="days"), ACCOUNT, [], last_price=100)
    assert order.qty * (order.limit_price - order.stop_price) == pytest.approx(50)


# ---- exposure, cash, size -------------------------------------------------

def test_total_exposure_cap_counts_open_and_pending(db, tmp_path):
    rm = make(db, tmp_path)
    active = [bracket(qty=10, price=100), bracket(symbol="SOL/USDT", qty=5, price=100, status="pending")]
    order, _ = rm.approve(intent(), ACCOUNT, active, last_price=100)
    assert order.notional == pytest.approx(2000 - 1500)


def test_exposure_cap_full_rejects(db, tmp_path):
    rm = make(db, tmp_path)
    order, reason = rm.approve(intent(), ACCOUNT, [bracket(qty=20, price=100)], last_price=100)
    assert order is None and "exposure limit" in reason


def test_closed_positions_do_not_count_as_exposure(db, tmp_path):
    rm = make(db, tmp_path)
    order, _ = rm.approve(intent(), ACCOUNT, [bracket(qty=20, price=100, status="closed")], last_price=100)
    assert order is not None


def test_cash_limits_long_size(db, tmp_path):
    rm = make(db, tmp_path)
    order, _ = rm.approve(intent(), AccountSnapshot(equity=10_000, cash=300), [], last_price=100)
    assert order.notional == pytest.approx(300)


def test_below_minimum_order_rejected(db, tmp_path):
    rm = make(db, tmp_path, min_order_notional=10)
    order, reason = rm.approve(intent(), AccountSnapshot(equity=10_000, cash=5), [], last_price=100)
    assert order is None and "minimum order" in reason


def test_one_position_per_symbol(db, tmp_path):
    rm = make(db, tmp_path)
    order, reason = rm.approve(intent("ETH/USDT"), ACCOUNT, [bracket("ETH/USDT", qty=1)], last_price=100)
    assert order is None and "already have" in reason


def test_invalid_inputs_rejected(db, tmp_path):
    rm = make(db, tmp_path)
    assert rm.approve(intent(), ACCOUNT, [], last_price=0)[0] is None
    assert rm.approve(intent(), AccountSnapshot(0, 0), [], last_price=100)[0] is None
    assert rm.approve(intent(horizon="weeks"), ACCOUNT, [], last_price=100)[0] is None
    assert rm.approve(intent(side="sideways"), ACCOUNT, [], last_price=100)[0] is None


# ---- shorts ---------------------------------------------------------------

def test_shorts_blocked_unless_config_and_broker_allow(db, tmp_path):
    assert make(db, tmp_path).approve(intent(side="short"), ACCOUNT, [], 100)[0] is None
    rm = make(db, tmp_path, allow_short=True, supports_short=False)
    assert rm.approve(intent(side="short"), ACCOUNT, [], 100)[0] is None   # spot broker can't short


def test_short_has_stop_above_and_target_below(db, tmp_path):
    rm = make(db, tmp_path, allow_short=True, supports_short=True)
    order, _ = rm.approve(intent(side="short"), ACCOUNT, [], 100)
    assert order.stop_price == pytest.approx(102) and order.take_profit_price == pytest.approx(96)


# ---- trades per day ---------------------------------------------------------

def test_max_trades_per_day_and_reset_next_day(db, tmp_path):
    clock = Clock()
    rm = make(db, tmp_path, clock=clock, max_trades_per_day=2)
    assert rm.approve(intent("BTC/USDT"), ACCOUNT, [], 100)[0]
    assert rm.approve(intent("ETH/USDT"), ACCOUNT, [], 100)[0]
    order, reason = rm.approve(intent("SOL/USDT"), ACCOUNT, [], 100)
    assert order is None and "max trades" in reason
    clock.t = T0 + timedelta(days=1)
    rm.update(ACCOUNT)
    assert rm.approve(intent("SOL/USDT"), ACCOUNT, [], 100)[0]


def test_trade_count_survives_restart(db, tmp_path):
    rm = make(db, tmp_path, max_trades_per_day=1)
    rm.approve(intent(), ACCOUNT, [], 100)
    rm2 = make(db, tmp_path, max_trades_per_day=1)          # same database, new process
    assert rm2.approve(intent("ETH/USDT"), ACCOUNT, [], 100)[0] is None


# ---- daily loss limit -------------------------------------------------------

def test_daily_loss_limit_flattens_and_halts_until_next_day(db, tmp_path):
    clock = Clock()
    rm = make(db, tmp_path, clock=clock)
    assert rm.update(AccountSnapshot(9_701, 9_701)) == []                   # -2.99%: fine
    actions = rm.update(AccountSnapshot(9_700, 9_700))                      # -3%: limit
    assert actions == [RiskAction.DAILY_LOSS_HIT, RiskAction.FLATTEN_DAILY_LOSS]
    assert rm.update(AccountSnapshot(9_800, 9_800)) == [RiskAction.FLATTEN_DAILY_LOSS]  # keeps flattening
    order, reason = rm.approve(intent(), ACCOUNT, [], 100)
    assert order is None and "daily loss" in reason
    clock.t = T0 + timedelta(days=1)
    assert rm.update(AccountSnapshot(9_800, 9_800)) == []                  # new day: halt lifted
    assert rm.approve(intent(), ACCOUNT, [], 100)[0] is not None


def test_daily_loss_measured_from_start_of_day_equity(db, tmp_path):
    clock = Clock()
    rm = make(db, tmp_path, clock=clock)
    clock.t = T0 + timedelta(days=1)
    rm.update(AccountSnapshot(8_000, 8_000))                                # new day starts at 8,000
    assert rm.update(AccountSnapshot(7_800, 7_800)) == []                  # -2.5% of 8,000
    assert RiskAction.DAILY_LOSS_HIT in rm.update(AccountSnapshot(7_760, 7_760))


# ---- kill switch ------------------------------------------------------------

def test_kill_file_flattens_blocks_and_needs_manual_resume(db, tmp_path):
    rm = make(db, tmp_path)
    (tmp_path / "KILL").touch()
    assert rm.update(ACCOUNT) == [RiskAction.KILL_HIT, RiskAction.FLATTEN_KILL]
    assert rm.update(ACCOUNT) == [RiskAction.FLATTEN_KILL]
    (tmp_path / "KILL").unlink()                    # deleting the file is not enough
    assert rm.update(ACCOUNT) == [RiskAction.FLATTEN_KILL]
    order, reason = rm.approve(intent(), ACCOUNT, [], 100)
    assert order is None and "kill switch" in reason
    rm.resume()
    assert rm.update(ACCOUNT) == []
    assert rm.approve(intent(), ACCOUNT, [], 100)[0] is not None


def test_kill_command_creates_file(db, tmp_path):
    rm = make(db, tmp_path)
    rm.kill("manual")
    assert (tmp_path / "KILL").exists()
    assert rm.block_reason() == "kill switch active (manual)"


def test_kill_file_blocks_even_before_next_update(db, tmp_path):
    rm = make(db, tmp_path)
    (tmp_path / "KILL").touch()
    assert rm.approve(intent(), ACCOUNT, [], 100)[0] is None


# ---- session window -----------------------------------------------------------

NY = SessionConfig(enabled=True)


def at(h, m, day=2):  # March 2026 in New York is EST (UTC-5) until the 8th
    return datetime(2026, 3, day, h + 5, m, tzinfo=timezone.utc)


@pytest.mark.parametrize("when,blocked", [
    (at(9, 0), "market closed"), (at(9, 30), "first 5"), (at(9, 34), "first 5"), (at(9, 35), None),
    (at(15, 54), None), (at(15, 55), "last 5"), (at(16, 0), "market closed"),
    (at(12, 0, day=7), "not a trading day"),                       # Saturday
])
def test_session_window(when, blocked):
    reason = session_block_reason(when, NY)
    assert (reason is None) if blocked is None else (blocked in reason)


def test_session_disabled_for_24_7_markets():
    assert session_block_reason(at(3, 0, day=7), SessionConfig(enabled=False)) is None


def test_session_blocks_approval(db, tmp_path):
    rm = make(db, tmp_path, clock=Clock(at(9, 31)), session=NY)
    order, reason = rm.approve(intent(), ACCOUNT, [], 100)
    assert order is None and "first 5" in reason


# ---- cannot be bypassed ---------------------------------------------------------

def test_cannot_construct_approved_order_directly():
    with pytest.raises(PermissionError):
        ApprovedOrder(1, "BTC/USDT", "long", 1e6, 100, 98, 104, "hours", T0, T0, 0.9, "x", None, _token=object())


def test_modified_order_is_refused_by_verify(db, tmp_path):
    rm = make(db, tmp_path)
    order, _ = rm.approve(intent(), ACCOUNT, [], 100)
    bigger = dataclasses.replace(order, qty=order.qty * 100)    # passes __post_init__ (same token) ...
    assert "not issued" in rm.verify(bigger)                    # ... but verify knows it isn't the original
    assert "not issued" in rm.verify(order)                     # and the original was consumed by that attempt


def test_verify_is_single_use(db, tmp_path):
    rm = make(db, tmp_path)
    order, _ = rm.approve(intent(), ACCOUNT, [], 100)
    assert rm.verify(order) is None
    assert "already used" in rm.verify(order)


def test_verify_rechecks_kill_switch_and_expiry(db, tmp_path):
    clock = Clock()
    rm = make(db, tmp_path, clock=clock)
    order, _ = rm.approve(intent(), ACCOUNT, [], 100)
    rm.kill("test")
    assert "kill switch" in rm.verify(order)
    rm.resume()
    order, _ = rm.approve(intent("ETH/USDT"), ACCOUNT, [], 100)
    clock.t = T0 + timedelta(minutes=10)
    assert rm.verify(order) == "approval expired"


def test_approval_requires_todays_state(db, tmp_path):
    clock = Clock()
    rm = make(db, tmp_path, clock=clock)
    clock.t = T0 + timedelta(days=1)                            # update() not run yet today
    order, reason = rm.approve(intent(), ACCOUNT, [], 100)
    assert order is None and "update()" in reason
