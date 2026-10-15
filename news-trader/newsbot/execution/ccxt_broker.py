"""Live market data via ccxt, and a spot broker for exchange testnet / live trading.

The spot broker is long-only. After a limit entry fills it protects the position
on the exchange: on Binance with one OCO order (take-profit LIMIT_MAKER above,
STOP_LOSS_LIMIT below, one cancels the other), elsewhere with a stop-loss order
only (the executor watches the take-profit). The executor also watches the stop
itself as a backup, for example when a fast move skips a stop-limit order.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable

import ccxt

from ..risk.manager import AccountSnapshot
from ..storage.db import Bracket, utcnow
from .broker import Broker, finish
from .sim import PriceFeed, PriceRange

log = logging.getLogger(__name__)


class CcxtFeed(PriceFeed):
    """Public market data (no API keys needed)."""

    def __init__(self, exchange, clock: Callable[[], datetime] = utcnow, cache_seconds: float = 3.0):
        self.exchange = exchange
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, float]] = {}

    def last_price(self, symbol: str) -> float:
        hit = self._cache.get(symbol)
        if hit and time.monotonic() - hit[0] < self.cache_seconds:
            return hit[1]
        t = self.exchange.fetch_ticker(symbol)
        price = t.get("last") or t.get("close")
        if not price:
            raise RuntimeError(f"no price for {symbol}")
        self._cache[symbol] = (time.monotonic(), float(price))
        return float(price)

    def bars(self, symbol: str, timeframe: str, limit: int) -> list[list]:
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        now_ms = int(self.clock().timestamp() * 1000)
        rows = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit + 1)
        return [r for r in rows if r[0] + tf_ms <= now_ms][-limit:]

    def range_since(self, symbol: str, since: datetime) -> PriceRange | None:
        since_ms = int(since.timestamp() * 1000)
        rows = [r for r in self.exchange.fetch_ohlcv(symbol, "1m", since=since_ms, limit=500) if r[0] >= since_ms]
        last = self.last_price(symbol)
        if not rows:
            return PriceRange(last, last, last, last)
        return PriceRange(rows[0][1], max(max(r[2] for r in rows), last),
                          min(min(r[3] for r in rows), last), last)


class CcxtSpotBroker(Broker):
    name = "ccxt-spot"
    supports_short = False

    def __init__(self, exchange, quote: str, symbols: list[str], stop_limit_offset_pct: float = 0.005,
                 clock: Callable[[], datetime] = utcnow):
        self.exchange = exchange
        self.quote = quote
        self.symbols = symbols
        self.stop_limit_offset_pct = stop_limit_offset_pct
        self.clock = clock
        self.feed = CcxtFeed(exchange, clock)
        exchange.load_markets()
        self.use_oco = getattr(exchange, "id", "") == "binance"

    # ---- data / account ----

    def last_price(self, symbol: str) -> float:
        return self.feed.last_price(symbol)

    def bars(self, symbol: str, timeframe: str, limit: int) -> list[list]:
        return self.feed.bars(symbol, timeframe, limit)

    def account(self) -> AccountSnapshot:
        bal = self.exchange.fetch_balance()
        total, free = bal.get("total", {}), bal.get("free", {})
        equity = float(total.get(self.quote) or 0)
        for sym in self.symbols:
            base = self.exchange.market(sym)["base"]
            held = float(total.get(base) or 0)
            if held:
                equity += held * self.last_price(sym)
        return AccountSnapshot(equity=equity, cash=float(free.get(self.quote) or 0))

    # ---- helpers ----

    def _amount(self, symbol: str, qty: float, price: float) -> float | None:
        amount = float(self.exchange.amount_to_precision(symbol, qty))
        limits = self.exchange.market(symbol).get("limits", {})
        if amount <= 0 or amount < ((limits.get("amount") or {}).get("min") or 0) \
                or amount * price < ((limits.get("cost") or {}).get("min") or 0):
            return None
        return amount

    def _price(self, symbol: str, price: float) -> str:
        return self.exchange.price_to_precision(symbol, price)

    def _fill_of(self, symbol: str, order: dict, side: str) -> tuple[float, float, float]:
        """(qty, avg price, fee in quote) of what an order executed."""
        market = self.exchange.market(symbol)
        qty = float(order.get("filled") or 0)
        price = float(order.get("average") or order.get("price") or 0)
        fee_quote = 0.0
        for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            cost = float(fee.get("cost") or 0)
            if fee.get("currency") == self.quote:
                fee_quote += cost
            elif fee.get("currency") == market["base"]:
                fee_quote += cost * price
                if side == "buy":
                    qty -= cost
        return qty, price, fee_quote

    def _cancel(self, symbol: str, order_id: str) -> dict | None:
        """Cancel if still open; return the final order (None if unknown to the exchange)."""
        try:
            self.exchange.cancel_order(order_id, symbol)
        except ccxt.AuthenticationError:
            raise
        except ccxt.ExchangeError:
            pass  # already filled/cancelled (e.g. the other leg of an OCO)
        try:
            order = self.exchange.fetch_order(order_id, symbol)
        except ccxt.OrderNotFound:
            return None
        if order.get("status") == "open":
            raise RuntimeError(f"order {order_id} still open after cancel")
        return order

    # ---- lifecycle ----

    def submit_entry(self, b: Bracket) -> None:
        if b.side != "long":
            raise RuntimeError("spot broker is long-only")
        amount = self._amount(b.symbol, b.qty, b.limit_price)
        if amount is None:
            raise RuntimeError(f"order below exchange minimum ({b.qty} {b.symbol})")
        order = self.exchange.create_order(b.symbol, "limit", "buy", amount, float(self._price(b.symbol, b.limit_price)))
        b.qty = amount
        b.entry_order_id = str(order["id"])

    def refresh(self, b: Bracket) -> None:
        if b.status == "pending":
            order = self.exchange.fetch_order(b.entry_order_id, b.symbol)
            status = order.get("status")
            if status == "closed":
                self._on_filled(b, order)
            elif status in ("canceled", "expired", "rejected"):
                if float(order.get("filled") or 0) > 0:
                    self._on_filled(b, order)
                else:
                    b.status, b.exit_reason, b.closed_at = "canceled", f"entry {status}", self.clock()
        elif b.status == "open":
            legs = [x for x in (b.stop_order_id, b.tp_order_id) if x]
            if not legs:
                self._protect(b)
                return
            orders = {oid: self.exchange.fetch_order(oid, b.symbol) for oid in legs}
            if all(o.get("status") == "open" for o in orders.values()):
                return
            if any(float(o.get("filled") or 0) > 0 for o in orders.values()):
                self._close(b, "exchange order", market_rest=True)
            else:  # cancelled outside the bot, nothing sold: put protection back
                for oid in legs:
                    self._cancel(b.symbol, oid)
                b.stop_order_id = b.tp_order_id = ""
                self._protect(b)

    def _on_filled(self, b: Bracket, order: dict) -> None:
        qty, price, fee = self._fill_of(b.symbol, order, "buy")
        b.qty, b.entry_price, b.entry_fee = qty, price, fee
        b.status, b.filled_at = "open", self.clock()
        self._protect(b)

    def _protect(self, b: Bracket) -> None:
        last = self.last_price(b.symbol)
        if last <= b.stop_price:
            return  # already through the stop: the executor sells at market
        base = self.exchange.market(b.symbol)["base"]
        free = float(self.exchange.fetch_balance().get("free", {}).get(base) or 0)
        amount = self._amount(b.symbol, min(b.qty, free), b.stop_price)
        if amount is None:
            log.error("cannot protect %s: amount below exchange minimum", b.symbol)
            return
        stop_limit = b.stop_price * (1 - self.stop_limit_offset_pct)
        try:
            if self.use_oco and last < b.take_profit_price:
                resp = self.exchange.private_post_orderlist_oco({
                    "symbol": self.exchange.market(b.symbol)["id"], "side": "SELL",
                    "quantity": self.exchange.amount_to_precision(b.symbol, amount),
                    "aboveType": "LIMIT_MAKER", "abovePrice": self._price(b.symbol, b.take_profit_price),
                    "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": self._price(b.symbol, b.stop_price),
                    "belowPrice": self._price(b.symbol, stop_limit), "belowTimeInForce": "GTC"})
                ids = {r.get("type"): str(r.get("orderId")) for r in resp.get("orderReports") or []}
                if not ids.get("STOP_LOSS_LIMIT") or not ids.get("LIMIT_MAKER"):
                    for o in resp.get("orders") or []:
                        self._cancel(b.symbol, str(o.get("orderId")))
                    raise RuntimeError(f"OCO response not understood, orders cancelled: {resp}")
                b.stop_order_id, b.tp_order_id = ids["STOP_LOSS_LIMIT"], ids["LIMIT_MAKER"]
            else:
                order = self.exchange.create_stop_loss_order(
                    b.symbol, "limit", "sell", amount, float(self._price(b.symbol, stop_limit)),
                    float(self._price(b.symbol, b.stop_price)))
                b.stop_order_id = str(order["id"])
        except ccxt.AuthenticationError:
            raise
        except Exception as e:
            log.error("could not place protective orders for %s (%s); executor watches the stop", b.symbol, e)

    def _close(self, b: Bracket, reason: str, market_rest: bool) -> None:
        """Cancel protective orders, book what they sold, sell any remainder at market."""
        sold_qty, sold_value, fees, why = 0.0, 0.0, 0.0, reason
        for oid, label in ((b.stop_order_id, "stop-loss (exchange)"), (b.tp_order_id, "take-profit (exchange)")):
            if not oid:
                continue
            order = self._cancel(b.symbol, oid)
            if order and float(order.get("filled") or 0) > 0:
                q, p, f = self._fill_of(b.symbol, order, "sell")
                sold_qty, sold_value, fees, why = sold_qty + q, sold_value + q * p, fees + f, label
        b.stop_order_id = b.tp_order_id = ""
        rest = b.qty - sold_qty
        if market_rest and rest > 0:
            base = self.exchange.market(b.symbol)["base"]
            free = float(self.exchange.fetch_balance().get("free", {}).get(base) or 0)
            last = self.last_price(b.symbol)
            amount = self._amount(b.symbol, min(rest, free), last)
            if amount is not None:
                order = self.exchange.create_order(b.symbol, "market", "sell", amount)
                if not (order.get("filled") and order.get("average")):
                    order = self.exchange.fetch_order(order["id"], b.symbol)
                q, p, f = self._fill_of(b.symbol, order, "sell")
                sold_qty, sold_value, fees = sold_qty + q, sold_value + q * p, fees + f
                why = reason if why == "exchange order" else why
            elif sold_qty == 0:
                raise RuntimeError(f"cannot close {b.symbol}: remaining amount below exchange minimum")
        if sold_qty <= 0:
            raise RuntimeError(f"close of {b.symbol} sold nothing")
        finish(b, sold_value / sold_qty, fees, why, self.clock())

    def cancel_entry(self, b: Bracket) -> None:
        if b.status != "pending":
            return
        order = self._cancel(b.symbol, b.entry_order_id)
        if order and float(order.get("filled") or 0) > 0:
            self._on_filled(b, order)   # keep the part that filled, protected
        else:
            b.status, b.exit_reason, b.closed_at = "canceled", "entry not filled in time", self.clock()

    def exit(self, b: Bracket, reason: str) -> None:
        if b.status == "pending":
            self.cancel_entry(b)
        if b.status == "open":
            self._close(b, reason, market_rest=True)

    def cancel_all_orders(self, symbols: list[str]) -> None:
        for sym in symbols:
            try:
                for o in self.exchange.fetch_open_orders(sym):
                    self._cancel(sym, str(o["id"]))
            except Exception as e:
                log.error("kill switch: could not cancel open orders on %s: %s", sym, e)
