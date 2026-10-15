"""Replay history bar by bar with the same strategy and risk rules the live bot uses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import CostConfig
from .fills import simulate_buy, simulate_sell
from .risk import RiskManager
from .strategy import Signal


@dataclass
class BacktestResult:
    bars: int
    start_equity: float
    end_equity: float
    buy_hold_return: float
    max_drawdown: float
    fees_paid: float
    halted: bool
    trades: list[dict] = field(default_factory=list)

    @property
    def total_return(self) -> float:
        return self.end_equity / self.start_equity - 1

    @property
    def win_rate(self) -> float:
        return sum(t["pnl"] > 0 for t in self.trades) / len(self.trades) if self.trades else 0.0

    def report(self) -> str:
        lines = [
            f"Bars tested:        {self.bars}",
            f"Start equity:       {self.start_equity:,.2f}",
            f"End equity:         {self.end_equity:,.2f}",
            f"Total return:       {self.total_return:+.2%}",
            f"Buy & hold return:  {self.buy_hold_return:+.2%}",
            f"Max drawdown:       {self.max_drawdown:.2%}",
            f"Trades:             {len(self.trades)}",
            f"Win rate:           {self.win_rate:.1%}",
            f"Fees paid:          {self.fees_paid:,.2f}",
        ]
        if self.halted:
            lines.append("Kill switch:        TRIGGERED (max drawdown) - bot stopped trading")
        return "\n".join(lines)


def run_backtest(candles: list[list], strategy, risk: RiskManager, costs: CostConfig,
                 starting_cash: float) -> BacktestResult:
    """Signals on bar i are executed at bar i+1's open (no look-ahead).
    If a bar touches both stop and take-profit, the stop is assumed to hit first (conservative)."""
    if len(candles) < strategy.min_bars + 1:
        raise ValueError(f"need at least {strategy.min_bars + 1} candles, got {len(candles)}")
    signals = strategy.signals([c[4] for c in candles])
    cash = starting_cash
    pos = None  # dict(qty, entry, entry_fee, stop, tp)
    trades: list[dict] = []
    fees = 0.0
    peak = starting_cash
    max_dd = 0.0
    day, day_start = None, starting_cash
    halted = False

    def close(ts, price, reason):
        nonlocal cash, pos, fees
        fill = simulate_sell(pos["qty"], price, costs)
        cash += fill.qty * fill.price - fill.fee
        fees += fill.fee
        pnl = fill.qty * fill.price - fill.fee - (pos["qty"] * pos["entry"] + pos["entry_fee"])
        trades.append({"opened": pos["opened"], "closed": ts, "entry_price": pos["entry"],
                       "exit_price": fill.price, "qty": fill.qty, "pnl": pnl, "reason": reason})
        pos = None

    for i in range(1, len(candles)):
        ts, o, h, l, c, _ = candles[i]
        equity_open = cash + (pos["qty"] * o if pos else 0)
        bar_day = datetime.fromtimestamp(ts / 1000, timezone.utc).date()
        if bar_day != day:
            day, day_start = bar_day, equity_open

        sig = signals[i - 1]
        if not halted:
            if sig == Signal.SELL and pos:
                close(ts, o, "sell signal")
            elif sig == Signal.BUY and not pos and not risk.daily_loss_hit(day_start, equity_open):
                qty = risk.position_size(equity_open, cash, o, costs)
                fill = simulate_buy(qty, o, cash, costs) if qty > 0 else None
                if fill:
                    cash -= fill.qty * fill.price + fill.fee
                    fees += fill.fee
                    pos = {"qty": fill.qty, "entry": fill.price, "entry_fee": fill.fee, "opened": ts,
                           "stop": risk.stop_price(fill.price), "tp": risk.take_profit_price(fill.price)}

        if pos:
            if l <= pos["stop"]:
                close(ts, min(o, pos["stop"]), "stop-loss")  # a gap down fills at the open
            elif pos["tp"] and h >= pos["tp"]:
                close(ts, max(o, pos["tp"]), "take-profit")

        equity = cash + (pos["qty"] * c if pos else 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        if not halted and risk.drawdown_hit(peak, equity):
            if pos:
                close(ts, c, "drawdown kill switch")
            halted = True

    if pos:
        close(candles[-1][0], candles[-1][4], "end of data")

    return BacktestResult(
        bars=len(candles), start_equity=starting_cash, end_equity=cash,
        buy_hold_return=candles[-1][4] / candles[0][4] - 1, max_drawdown=max_dd,
        fees_paid=fees, halted=halted, trades=trades)
