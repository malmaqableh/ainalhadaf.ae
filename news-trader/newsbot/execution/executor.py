"""Places risk-approved orders and manages every bracket until it closes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from ..risk.manager import ApprovedOrder, RiskManager
from ..storage.db import Bracket, Database, utcnow
from .broker import Broker

log = logging.getLogger(__name__)


class Executor:
    def __init__(self, db: Database, broker: Broker, risk: RiskManager,
                 notify: Callable[[str, str], None] | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.db = db
        self.broker = broker
        self.risk = risk
        self.notify = notify or (lambda kind, msg: None)   # kind: trade | error | risk
        self.clock = clock

    # ---- opening ---------------------------------------------------------

    def open(self, order: ApprovedOrder) -> Bracket | None:
        if not isinstance(order, ApprovedOrder):
            raise TypeError("Executor.open only accepts an ApprovedOrder from RiskManager")
        if (problem := self.risk.verify(order)):
            self.db.log_decision("execution", "refused", problem, order.symbol)
            log.warning("order refused at submission: %s", problem)
            return None
        b = Bracket(symbol=order.symbol, side=order.side, status="pending", qty=order.qty,
                    limit_price=order.limit_price, stop_price=order.stop_price,
                    take_profit_price=order.take_profit_price, entry_deadline=order.entry_deadline,
                    max_hold_until=order.max_hold_until, event_id=order.event_id, horizon=order.horizon,
                    confidence=order.confidence, reason=order.reason, created_at=self.clock())
        with self.db.session() as s:
            s.add(b)
            s.commit()
        try:
            self.broker.submit_entry(b)
        except Exception as e:
            b.status, b.exit_reason, b.closed_at = "canceled", f"submit failed: {e}", self.clock()
            self.db.save(b)
            self.db.log_decision("execution", "submit_failed", str(e), b.symbol)
            self.notify("error", f"Order for {b.symbol} failed: {e}")
            return None
        self.db.save(b)
        self.db.log_decision("execution", "entry_submitted",
                             f"limit {b.side} {b.qty:.6f} @ {b.limit_price:.4f} (stop {b.stop_price:.4f}, "
                             f"target {b.take_profit_price:.4f}) - {b.reason}", b.symbol, {"bracket": b.id})
        return b

    # ---- managing ----------------------------------------------------------

    def manage(self) -> None:
        """Advance every active bracket. Errors on one bracket don't stop the others."""
        for b in self.db.active_brackets():
            before = b.status
            try:
                self._manage_one(b)
            except Exception as e:
                log.exception("managing bracket %s (%s) failed", b.id, b.symbol)
                self.notify("error", f"Managing {b.symbol} position failed: {e}")
            finally:
                self.db.save(b)
            self._report(b, before)

    def _manage_one(self, b: Bracket) -> None:
        now = self.clock()
        self.broker.refresh(b)
        if b.status == "pending" and now >= b.entry_deadline:
            self.broker.cancel_entry(b)
        if b.status != "open":
            return
        if b.max_hold_until and now >= b.max_hold_until:
            self.broker.exit(b, "max holding time reached")
            return
        # Backup to the exchange's own orders: if price is through the stop or target and
        # the position is still open (e.g. a stop-limit was skipped), close it now.
        price = self.broker.last_price(b.symbol)
        long = b.side == "long"
        if (price <= b.stop_price) if long else (price >= b.stop_price):
            self.broker.exit(b, "stop-loss")
        elif (price >= b.take_profit_price) if long else (price <= b.take_profit_price):
            self.broker.exit(b, "take-profit")

    def _report(self, b: Bracket, before: str) -> None:
        if b.status == before:
            return
        if b.status == "open":
            msg = f"FILLED {b.side} {b.qty:.6f} {b.symbol} @ {b.entry_price:.4f}"
            self.db.log_decision("execution", "entry_filled", msg, b.symbol, {"bracket": b.id})
            self.notify("trade", msg)
        elif b.status == "closed":
            msg = (f"CLOSED {b.side} {b.symbol} @ {b.exit_price:.4f} ({b.exit_reason}), "
                   f"P&L {b.pnl:+.2f}")
            self.db.log_decision("execution", "closed", msg, b.symbol, {"bracket": b.id, "pnl": b.pnl})
            self.notify("trade", msg)
        elif b.status == "canceled":
            self.db.log_decision("execution", "entry_canceled", b.exit_reason, b.symbol, {"bracket": b.id})

    # ---- closing on purpose -----------------------------------------------------

    def close_symbol(self, symbol: str, reason: str) -> bool:
        """Close any position in `symbol` (e.g. strong bearish news on a held long)."""
        closed = False
        for b in self.db.active_brackets():
            if b.symbol == symbol:
                before = b.status
                self.broker.exit(b, reason)
                self.db.save(b)
                self._report(b, before)
                closed = True
        return closed

    def flatten_all(self, reason: str, symbols: list[str]) -> int:
        """Cancel every order and close every position. Returns how many are still open."""
        for b in self.db.active_brackets():
            before = b.status
            try:
                self.broker.exit(b, reason)
            except Exception as e:
                log.exception("flatten: closing %s failed", b.symbol)
                self.notify("error", f"Flatten ({reason}): closing {b.symbol} failed: {e}")
            self.db.save(b)
            self._report(b, before)
        try:
            self.broker.cancel_all_orders(symbols)
        except Exception as e:
            log.exception("flatten: cancel_all_orders failed")
            self.notify("error", f"Flatten ({reason}): cancelling orders failed: {e}")
        remaining = len(self.db.active_brackets())
        if remaining:
            log.error("flatten (%s): %d positions still open, will retry", reason, remaining)
        return remaining
