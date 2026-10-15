"""Backtest performance metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Report:
    start_equity: float
    end_equity: float
    max_drawdown: float
    sharpe: float | None
    trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float | None
    fees: float
    days: int

    @property
    def total_return(self) -> float:
        return self.end_equity / self.start_equity - 1

    def text(self) -> str:
        pf = "n/a" if self.profit_factor is None else f"{self.profit_factor:.2f}"
        sharpe = "n/a (need 2+ days with returns)" if self.sharpe is None else f"{self.sharpe:.2f}"
        return "\n".join([
            f"Period:          {self.days} days",
            f"Start equity:    {self.start_equity:,.2f}",
            f"End equity:      {self.end_equity:,.2f}",
            f"Total return:    {self.total_return:+.2%}",
            f"Max drawdown:    {self.max_drawdown:.2%}",
            f"Sharpe ratio:    {sharpe}",
            f"Trades:          {self.trades}",
            f"Win rate:        {self.win_rate:.1%}",
            f"Average win:     {self.avg_win:+,.2f}",
            f"Average loss:    {self.avg_loss:+,.2f}",
            f"Profit factor:   {pf}",
            f"Fees paid:       {self.fees:,.2f}",
        ])


def max_drawdown(equity: list[float]) -> float:
    peak, dd = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            dd = max(dd, 1 - e / peak)
    return dd


def sharpe_ratio(daily_equity: list[float], periods_per_year: int) -> float | None:
    """Annualised Sharpe of daily returns (risk-free rate 0)."""
    rets = [b / a - 1 for a, b in zip(daily_equity, daily_equity[1:]) if a > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var == 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(periods_per_year)


def build_report(curve: list[tuple[datetime, float]], pnls: list[float], fees: float,
                 periods_per_year: int = 365) -> Report:
    equity = [e for _, e in curve]
    daily: dict = {}
    for t, e in curve:
        daily[t.date()] = e  # last value of each day; starting equity is the baseline for day 1
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return Report(
        start_equity=equity[0], end_equity=equity[-1], max_drawdown=max_drawdown(equity),
        sharpe=sharpe_ratio([equity[0], *daily.values()], periods_per_year), trades=len(pnls),
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        fees=fees, days=len(daily))
