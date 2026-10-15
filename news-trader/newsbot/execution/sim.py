"""Simulated broker: the same fill rules for paper trading (live prices) and backtests (history).

Fill rules, per price range seen since the last check (open, high, low, last):
- Limit entry fills when price trades through the limit, at the limit (or the
  better opening price after a gap). No slippage on limit fills.
- Stop-loss fills when price touches the stop, at the stop (or the worse opening
  price after a gap), minus slippage.
- Take-profit is a limit order: fills at the target (or a better gap price).
- If one range touches both, the stop is assumed to hit first (conservative).
- Fees: costs.fee_rate on every fill.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..config import CostConfig
from ..risk.manager import AccountSnapshot
from ..storage.db import Bracket, utcnow
from .broker import Broker, finish

log = logging.getLogger(__name__)


@dataclass
class PriceRange:
    open: float
    high: float
    low: float
    last: float


class PriceFeed(ABC):
    @abstractmethod
    def last_price(self, symbol: str) -> float: ...

    @abstractmethod
    def bars(self, symbol: str, timeframe: str, limit: int) -> list[list]: ...

    @abstractmethod
    def range_since(self, symbol: str, since: datetime) -> PriceRange | None:
        """Prices traded after `since` (None if nothing new)."""


class SimulatedBroker(Broker):
    name = "simulated"

    def __init__(self, feed: PriceFeed, costs: CostConfig, starting_cash: float,
                 supports_short: bool = True, state_path: str | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.feed = feed
        self.costs = costs
        self.supports_short = supports_short
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        self.cash = starting_cash
        self.positions: dict[str, dict] = {}   # bracket id -> {symbol, side, qty}
        self.checked: dict[str, str] = {}      # bracket id -> ISO time of last fill check
        self._load()

    # ---- persistence (paper account survives restarts) ----

    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            data = json.loads(self.state_path.read_text())
            self.cash = data["cash"]
            self.positions = data["positions"]
            self.checked = data.get("checked", {})

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cash": self.cash, "positions": self.positions, "checked": self.checked}))
        os.replace(tmp, self.state_path)

    # ---- market data / account ----

    def last_price(self, symbol: str) -> float:
        return self.feed.last_price(symbol)

    def bars(self, symbol: str, timeframe: str, limit: int) -> list[list]:
        return self.feed.bars(symbol, timeframe, limit)

    def account(self) -> AccountSnapshot:
        equity = self.cash
        for p in self.positions.values():
            value = p["qty"] * self.feed.last_price(p["symbol"])
            equity += value if p["side"] == "long" else -value
        return AccountSnapshot(equity=equity, cash=max(self.cash, 0.0))

    # ---- bracket lifecycle ----

    def submit_entry(self, b: Bracket) -> None:
        if b.side == "short" and not self.supports_short:
            raise RuntimeError("short selling not supported")
        b.entry_order_id = f"sim-{b.id}"
        self.checked[str(b.id)] = self.clock().isoformat()
        self._save()

    def _range(self, b: Bracket) -> PriceRange | None:
        since = datetime.fromisoformat(self.checked.get(str(b.id), self.clock().isoformat()))
        r = self.feed.range_since(b.symbol, since)
        self.checked[str(b.id)] = self.clock().isoformat()
        return r

    def refresh(self, b: Bracket) -> None:
        if b.status not in ("pending", "open"):
            return
        r = self._range(b)
        if r is None:
            self._save()
            return
        long = b.side == "long"
        if b.status == "pending":
            touched = r.low <= b.limit_price if long else r.high >= b.limit_price
            if touched:
                price = min(b.limit_price, r.open) if long else max(b.limit_price, r.open)
                self._fill_entry(b, price)
        elif b.status == "open":
            stop_hit = r.low <= b.stop_price if long else r.high >= b.stop_price
            tp_hit = r.high >= b.take_profit_price if long else r.low <= b.take_profit_price
            if stop_hit:
                gap = min(r.open, b.stop_price) if long else max(r.open, b.stop_price)
                self._fill_exit(b, gap, "stop-loss", slippage=True)
            elif tp_hit:
                gap = max(r.open, b.take_profit_price) if long else min(r.open, b.take_profit_price)
                self._fill_exit(b, gap, "take-profit", slippage=False)
        self._save()

    def _fill_entry(self, b: Bracket, price: float) -> None:
        if b.side == "long":
            affordable = self.cash / (price * (1 + self.costs.fee_rate))
            b.qty = min(b.qty, affordable)
        if b.qty <= 0:
            b.status = "canceled"
            b.exit_reason = "insufficient cash"
            return
        fee = b.qty * price * self.costs.fee_rate
        self.cash += (-1 if b.side == "long" else 1) * b.qty * price - fee
        b.entry_price, b.entry_fee, b.status, b.filled_at = price, fee, "open", self.clock()
        self.positions[str(b.id)] = {"symbol": b.symbol, "side": b.side, "qty": b.qty}

    def _fill_exit(self, b: Bracket, price: float, reason: str, slippage: bool) -> None:
        if slippage:
            price *= (1 - self.costs.slippage) if b.side == "long" else (1 + self.costs.slippage)
        fee = b.qty * price * self.costs.fee_rate
        self.cash += (1 if b.side == "long" else -1) * b.qty * price - fee
        self.positions.pop(str(b.id), None)
        finish(b, price, fee, reason, self.clock())

    def cancel_entry(self, b: Bracket) -> None:
        if b.status == "pending":
            b.status = "canceled"
            b.exit_reason = "entry not filled in time"
            b.closed_at = self.clock()
            self.checked.pop(str(b.id), None)
            self._save()

    def exit(self, b: Bracket, reason: str) -> None:
        if b.status == "pending":
            self.cancel_entry(b)
            return
        if b.status != "open":
            return
        self._fill_exit(b, self.feed.last_price(b.symbol), reason, slippage=True)
        self._save()
