import pytest

from bot.config import CostConfig, RiskConfig
from bot.risk import RiskManager
from bot.strategy import MACrossover, Signal, sma


def test_sma():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]


def test_crossover_signals():
    s = MACrossover(2, 4)
    closes = [10, 10, 10, 10, 10, 12, 12, 12, 8, 6]
    sig = s.signals(closes)
    assert sig[5] == Signal.BUY
    assert sig[8] == Signal.SELL
    assert [x for x in sig if x != Signal.HOLD] == [Signal.BUY, Signal.SELL]


def test_signal_uses_latest_bar_only():
    s = MACrossover(2, 4)
    assert s.signal([10, 10, 10, 10, 10, 12]) == Signal.BUY
    assert s.signal([10, 10, 10, 10, 10, 12, 12]) == Signal.HOLD
    assert s.signal([10, 12]) == Signal.HOLD  # not enough data


def test_invalid_periods():
    with pytest.raises(ValueError):
        MACrossover(50, 20)


def test_position_size_respects_every_limit():
    risk = RiskManager(RiskConfig(risk_per_trade=0.01, stop_loss_pct=0.05, max_position_pct=0.5))
    no_costs = CostConfig(fee_rate=0, slippage=0)
    # risk limit: 1000*0.01 / (100*0.05) = 2 units; cap: 1000*0.5/100 = 5 units
    assert risk.position_size(1000, 1000, 100, no_costs) == pytest.approx(2)
    # cash limit wins when cash is short
    assert risk.position_size(1000, 50, 100, no_costs) == pytest.approx(0.5)
    # max position cap wins with a very wide risk budget
    wide = RiskManager(RiskConfig(risk_per_trade=0.5, stop_loss_pct=0.05, max_position_pct=0.25))
    assert wide.position_size(1000, 1000, 100, no_costs) == pytest.approx(2.5)
    assert risk.position_size(0, 1000, 100, no_costs) == 0


def test_limits():
    risk = RiskManager(RiskConfig(max_daily_loss_pct=0.03, max_drawdown_pct=0.15))
    assert risk.daily_loss_hit(1000, 970)
    assert not risk.daily_loss_hit(1000, 971)
    assert risk.drawdown_hit(1000, 850)
    assert not risk.drawdown_hit(1000, 851)
    assert risk.stop_price(100) == pytest.approx(97)
