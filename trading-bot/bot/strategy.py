"""Trading strategies. A strategy turns a list of closed-candle prices into signals."""

from __future__ import annotations

from enum import Enum


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average; None until enough values exist."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    window_sum = 0.0
    for i, v in enumerate(values):
        window_sum += v
        if i >= period:
            window_sum -= values[i - period]
        if i >= period - 1:
            out[i] = window_sum / period
    return out


class MACrossover:
    """Buy when the fast average crosses above the slow one; sell when it crosses below."""

    def __init__(self, fast: int, slow: int):
        if not (0 < fast < slow):
            raise ValueError("fast must be > 0 and smaller than slow")
        self.fast = fast
        self.slow = slow

    @property
    def min_bars(self) -> int:
        # One extra bar so there is a previous value to detect the cross.
        return self.slow + 1

    def signals(self, closes: list[float]) -> list[Signal]:
        """Signal for every bar, using only that bar and earlier ones."""
        fast = sma(closes, self.fast)
        slow = sma(closes, self.slow)
        out = [Signal.HOLD] * len(closes)
        for i in range(1, len(closes)):
            pf, ps, f, s = fast[i - 1], slow[i - 1], fast[i], slow[i]
            if None in (pf, ps, f, s):
                continue
            if pf <= ps and f > s:
                out[i] = Signal.BUY
            elif pf >= ps and f < s:
                out[i] = Signal.SELL
        return out

    def signal(self, closes: list[float]) -> Signal:
        """Signal for the latest closed bar."""
        if len(closes) < self.min_bars:
            return Signal.HOLD
        return self.signals(closes[-self.min_bars:])[-1]


def make_strategy(cfg) -> MACrossover:
    if cfg.name == "ma_crossover":
        return MACrossover(cfg.fast, cfg.slow)
    raise ValueError(f"unknown strategy {cfg.name!r}")
