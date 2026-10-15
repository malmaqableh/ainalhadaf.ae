"""The autonomous loop: watch the market, decide, trade, enforce limits, repeat."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import ccxt

from .config import Config
from .risk import RiskManager
from .state import BotState, Position, StateStore
from .strategy import Signal

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 900


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Engine:
    def __init__(self, cfg: Config, broker, strategy, risk: RiskManager, store: StateStore,
                 state: BotState, clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.store = store
        self.state = state
        self.clock = clock
        self.sleep = sleep
        self.timeframe_ms = ccxt.Exchange.parse_timeframe(cfg.timeframe) * 1000

    # ---- one iteration -------------------------------------------------

    def step(self) -> bool:
        """Run one check of the market. Returns False when the bot must stop."""
        if Path(self.cfg.kill_file).exists():
            log.warning("kill file %s found; stopping (open positions are left as they are)", self.cfg.kill_file)
            return False
        if self.state.halted:
            log.error("bot is halted: %s. Run `python -m bot resume` after reviewing.", self.state.halt_reason)
            return False

        now = self.clock()
        now_ms = int(now.timestamp() * 1000)
        candles = self.broker.fetch_candles(self.strategy.min_bars + 2)
        closed = [c for c in candles if c[0] + self.timeframe_ms <= now_ms]
        price = self.broker.last_price()
        equity = self.broker.equity(price)

        if not self.state.initialized:
            self.state.peak_equity = equity
            self.state.initialized = True
        if self.state.day != now.date().isoformat():
            self.state.day = now.date().isoformat()
            self.state.day_start_equity = equity
        self.state.peak_equity = max(self.state.peak_equity, equity)

        if self.risk.drawdown_hit(self.state.peak_equity, equity):
            reason = (f"max drawdown hit: equity {equity:.2f} vs peak {self.state.peak_equity:.2f}")
            log.error(reason)
            if self.state.position:
                self._exit(price, "drawdown kill switch")
            self.state.halted = True
            self.state.halt_reason = reason
            self.store.save(self.state)
            return False

        pos = self.state.position
        if pos and price <= pos.stop_price:
            self._exit(price, "stop-loss")
        elif pos and pos.take_profit_price and price >= pos.take_profit_price:
            self._exit(price, "take-profit")

        if closed and closed[-1][0] > self.state.last_candle_ts:
            self.state.last_candle_ts = closed[-1][0]
            signal = self.strategy.signal([c[4] for c in closed])
            log.info("candle closed @ %s price=%.2f equity=%.2f signal=%s",
                     datetime.fromtimestamp(closed[-1][0] / 1000, timezone.utc).isoformat(),
                     price, equity, signal.value)
            if signal == Signal.SELL and self.state.position:
                self._exit(price, "sell signal")
            elif signal == Signal.BUY and not self.state.position:
                if self.risk.daily_loss_hit(self.state.day_start_equity, equity):
                    log.warning("buy signal skipped: daily loss limit reached")
                else:
                    self._enter(price, equity)

        self.store.save(self.state)
        return True

    # ---- orders --------------------------------------------------------

    def _enter(self, price: float, equity: float) -> None:
        qty = self.risk.position_size(equity, self.broker.free_cash(), price, self.cfg.costs)
        if qty <= 0:
            log.warning("buy signal skipped: position size is zero")
            return
        fill = self.broker.buy(qty, price)
        if not fill:
            return
        self.state.position = Position(
            qty=fill.qty, entry_price=fill.price, entry_fee=fill.fee,
            stop_price=self.risk.stop_price(fill.price),
            take_profit_price=self.risk.take_profit_price(fill.price),
            opened_at=self.clock().isoformat())
        log.info("BUY %.8f %s @ %.2f (fee %.4f) stop=%.2f",
                 fill.qty, self.cfg.symbol, fill.price, fill.fee, self.state.position.stop_price)
        self.store.save(self.state)

    def _exit(self, price: float, reason: str) -> None:
        pos = self.state.position
        fill = self.broker.sell(pos.qty, price)
        if not fill:
            log.error("could not close position (%s); will retry next check", reason)
            return
        pnl = fill.qty * fill.price - fill.fee - (fill.qty * pos.entry_price + pos.entry_fee)
        self.state.trades.append({
            "symbol": self.cfg.symbol, "qty": fill.qty,
            "entry_price": pos.entry_price, "exit_price": fill.price,
            "opened_at": pos.opened_at, "closed_at": self.clock().isoformat(),
            "pnl": round(pnl, 8), "reason": reason})
        self.state.position = None
        log.info("SELL %.8f %s @ %.2f (%s) pnl=%.2f", fill.qty, self.cfg.symbol, fill.price, reason, pnl)
        self.store.save(self.state)

    # ---- forever loop --------------------------------------------------

    def run(self) -> None:
        log.info("bot started: mode=%s %s %s %s", self.cfg.mode, self.cfg.exchange,
                 self.cfg.symbol, self.cfg.timeframe)
        errors = 0
        try:
            while True:
                try:
                    if not self.step():
                        break
                    errors = 0
                    delay = self.cfg.poll_seconds
                except ccxt.AuthenticationError:
                    raise  # wrong keys: retrying will not help
                except Exception:
                    # Keep running: an open position still needs its stop-loss watched.
                    errors += 1
                    delay = min(self.cfg.poll_seconds * 2 ** errors, MAX_BACKOFF_SECONDS)
                    log.exception("error during check #%d in a row; retrying in %ss", errors, delay)
                self.sleep(delay)
        except KeyboardInterrupt:
            log.info("stopped by user")
        finally:
            self.store.save(self.state)
            log.info("bot stopped")
