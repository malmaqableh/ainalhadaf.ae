"""Broker interface. Implement this to add a broker (Alpaca, IBKR, OANDA...).

All trading goes through brackets: a limit entry order, then a stop-loss and a
take-profit once it fills. The executor drives the lifecycle; a broker only has
to move one bracket forward when asked.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..risk.manager import AccountSnapshot
from ..storage.db import Bracket


class Broker(ABC):
    name: str = "broker"
    supports_short: bool = False

    # ---- market data ----
    @abstractmethod
    def last_price(self, symbol: str) -> float: ...

    @abstractmethod
    def bars(self, symbol: str, timeframe: str, limit: int) -> list[list]:
        """Recent candles [ts_ms, open, high, low, close, volume], oldest first, closed bars only."""

    # ---- account ----
    @abstractmethod
    def account(self) -> AccountSnapshot: ...

    # ---- bracket lifecycle ----
    @abstractmethod
    def submit_entry(self, b: Bracket) -> None:
        """Place the limit entry order; set b.entry_order_id."""

    @abstractmethod
    def refresh(self, b: Bracket) -> None:
        """Move the bracket forward: detect the entry fill (then attach stop-loss and
        take-profit), detect an exit. Updates b in place."""

    @abstractmethod
    def cancel_entry(self, b: Bracket) -> None:
        """Cancel an unfilled entry. If part filled, keep that part as an open position."""

    @abstractmethod
    def exit(self, b: Bracket, reason: str) -> None:
        """Close an open position now (cancel its protective orders first)."""

    def cancel_all_orders(self, symbols: list[str]) -> None:
        """Kill switch: cancel every open order on these symbols. Optional."""


def finish(b: Bracket, price: float, fee: float, reason: str, when: datetime) -> None:
    """Mark a bracket closed and compute its P&L."""
    sign = 1 if b.side == "long" else -1
    b.exit_price = price
    b.exit_fee = fee
    b.exit_reason = reason
    b.pnl = sign * (price - (b.entry_price or 0)) * b.qty - b.entry_fee - fee
    b.status = "closed"
    b.closed_at = when
