"""Market data and order execution for paper, testnet and live modes."""

from __future__ import annotations

import logging
import os
import time

import ccxt

from .config import Config, CostConfig
from .fills import Fill, simulate_buy, simulate_sell
from .state import BotState

log = logging.getLogger(__name__)


class LiveTradingNotConfirmed(RuntimeError):
    pass


class ExchangeBroker:
    """Market data from a ccxt exchange object."""

    def __init__(self, exchange, symbol: str, timeframe: str):
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = timeframe

    def fetch_candles(self, limit: int) -> list[list]:
        """Most recent candles [ts_ms, open, high, low, close, volume]; the last may still be forming."""
        return self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=limit)

    def last_price(self) -> float:
        ticker = self.exchange.fetch_ticker(self.symbol)
        price = ticker.get("last") or ticker.get("close")
        if not price:
            raise RuntimeError(f"no last price for {self.symbol}")
        return float(price)


class PaperBroker(ExchangeBroker):
    """Real market prices, simulated money. The account lives in BotState."""

    supports_stop_orders = False

    def __init__(self, exchange, symbol: str, timeframe: str, costs: CostConfig, state: BotState):
        super().__init__(exchange, symbol, timeframe)
        self.costs = costs
        self.state = state

    def equity(self, price: float) -> float:
        held = self.state.position.qty * price if self.state.position else 0.0
        return self.state.cash + held

    def free_cash(self) -> float:
        return self.state.cash

    def buy(self, qty: float, ref_price: float) -> Fill | None:
        fill = simulate_buy(qty, ref_price, self.state.cash, self.costs)
        if fill:
            self.state.cash -= fill.qty * fill.price + fill.fee
        return fill

    def sell(self, qty: float, ref_price: float) -> Fill | None:
        fill = simulate_sell(qty, ref_price, self.costs)
        if fill:
            self.state.cash += fill.qty * fill.price - fill.fee
        return fill


class LiveBroker(ExchangeBroker):
    """Real orders on the exchange (or its testnet). The exchange balance is the source of truth."""

    supports_stop_orders = True

    def __init__(self, exchange, symbol: str, timeframe: str,
                 stop_order_type: str = "limit", stop_limit_offset_pct: float = 0.01):
        super().__init__(exchange, symbol, timeframe)
        self.stop_order_type = stop_order_type
        self.stop_limit_offset_pct = stop_limit_offset_pct
        exchange.load_markets()
        self.market = exchange.market(symbol)
        self.base = self.market["base"]
        self.quote = self.market["quote"]

    def _balances(self) -> dict:
        return self.exchange.fetch_balance()

    def equity(self, price: float) -> float:
        total = self._balances().get("total", {})
        return float(total.get(self.quote) or 0) + float(total.get(self.base) or 0) * price

    def free_cash(self) -> float:
        return float(self._balances().get("free", {}).get(self.quote) or 0)

    def _tradeable_amount(self, qty: float, price: float) -> float | None:
        amount = float(self.exchange.amount_to_precision(self.symbol, qty))
        limits = self.market.get("limits", {})
        min_amount = (limits.get("amount") or {}).get("min") or 0
        min_cost = (limits.get("cost") or {}).get("min") or 0
        if amount <= 0 or amount < min_amount or amount * price < min_cost:
            log.warning("order of %s %s is below the exchange minimum; skipped", qty, self.base)
            return None
        return amount

    def _settle(self, order: dict) -> dict:
        """Market orders sometimes come back before fill details are known; poll briefly."""
        for _ in range(5):
            if order.get("filled") and order.get("average"):
                return order
            time.sleep(1)
            order = self.exchange.fetch_order(order["id"], self.symbol)
        return order

    def _to_fill(self, side: str, order: dict, ref_price: float) -> Fill | None:
        qty = float(order.get("filled") or 0)
        if qty <= 0:
            log.error("order %s reported no fill: %s", order.get("id"), order.get("status"))
            return None
        price = float(order.get("average") or order.get("price") or ref_price)
        fee_quote = 0.0
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for fee in fees:
            cost = float(fee.get("cost") or 0)
            if fee.get("currency") == self.quote:
                fee_quote += cost
            elif fee.get("currency") == self.base:
                fee_quote += cost * price
                if side == "buy":
                    qty -= cost  # fee taken from the coins received
            elif cost:
                log.info("fee of %s %s not counted in P&L", cost, fee.get("currency"))
        return Fill(side, qty, price, fee_quote)

    def buy(self, qty: float, ref_price: float) -> Fill | None:
        amount = self._tradeable_amount(qty, ref_price)
        if amount is None:
            return None
        order = self.exchange.create_order(self.symbol, "market", "buy", amount)
        return self._to_fill("buy", self._settle(order), ref_price)

    def sell(self, qty: float, ref_price: float) -> Fill | None:
        free_base = float(self._balances().get("free", {}).get(self.base) or 0)
        amount = self._tradeable_amount(min(qty, free_base), ref_price)
        if amount is None:
            return None
        order = self.exchange.create_order(self.symbol, "market", "sell", amount)
        return self._to_fill("sell", self._settle(order), ref_price)

    # ---- exchange-side stop-loss ---------------------------------------
    # Order status helpers return (status, fill): status is "open", "closed" (fully
    # filled) or "canceled"; fill covers whatever part of the order executed.

    def place_stop(self, qty: float, stop_price: float) -> str | None:
        """Rest a sell stop-loss order on the exchange; returns its order id."""
        trigger = float(self.exchange.price_to_precision(self.symbol, stop_price))
        limit = None
        if self.stop_order_type == "limit":
            limit = float(self.exchange.price_to_precision(
                self.symbol, stop_price * (1 - self.stop_limit_offset_pct)))
        free_base = float(self._balances().get("free", {}).get(self.base) or 0)
        amount = self._tradeable_amount(min(qty, free_base), limit or trigger)
        if amount is None:
            return None
        order = self.exchange.create_stop_loss_order(
            self.symbol, self.stop_order_type, "sell", amount, limit, trigger)
        return str(order["id"])

    def stop_order_status(self, order_id: str) -> tuple[str, Fill | None]:
        try:
            order = self.exchange.fetch_order(order_id, self.symbol)
        except ccxt.OrderNotFound:
            log.warning("stop order %s not found on the exchange", order_id)
            return "canceled", None
        status = order.get("status")
        if status == "open":
            return "open", None
        fill = None
        if float(order.get("filled") or 0) > 0:
            fill = self._to_fill("sell", order, float(order.get("price") or 0))
        return ("closed" if status == "closed" else "canceled"), fill

    def cancel_stop(self, order_id: str) -> tuple[str, Fill | None]:
        try:
            self.exchange.cancel_order(order_id, self.symbol)
        except ccxt.OrderNotFound:
            pass  # already filled or removed; the status below says which
        status, fill = self.stop_order_status(order_id)
        if status == "open":
            raise RuntimeError(f"stop order {order_id} is still open after cancelling")
        return status, fill


def make_exchange(cfg: Config, authenticated: bool):
    cls = getattr(ccxt, cfg.exchange, None)
    if cls is None:
        raise ValueError(f"unknown exchange {cfg.exchange!r} (see ccxt.exchanges)")
    params: dict = {"enableRateLimit": True}
    if authenticated:
        key, secret = os.environ.get("BOT_API_KEY"), os.environ.get("BOT_API_SECRET")
        if not key or not secret:
            raise LiveTradingNotConfirmed("BOT_API_KEY and BOT_API_SECRET must be set for testnet/live mode")
        params.update(apiKey=key, secret=secret)
        if os.environ.get("BOT_API_PASSWORD"):
            params["password"] = os.environ["BOT_API_PASSWORD"]
    exchange = cls(params)
    if cfg.mode == "testnet":
        exchange.set_sandbox_mode(True)  # raises NotSupported if the exchange has no testnet
    return exchange


def make_broker(cfg: Config, state: BotState):
    if cfg.mode == "paper":
        return PaperBroker(make_exchange(cfg, authenticated=False), cfg.symbol, cfg.timeframe, cfg.costs, state)
    if cfg.mode == "live" and os.environ.get("BOT_CONFIRM_LIVE") != "YES":
        raise LiveTradingNotConfirmed(
            "mode is 'live' but BOT_CONFIRM_LIVE=YES is not set. Real money is at risk; "
            "set it only after backtesting and paper/testnet trading.")
    return LiveBroker(make_exchange(cfg, authenticated=True), cfg.symbol, cfg.timeframe,
                      cfg.risk.stop_order_type, cfg.risk.stop_limit_offset_pct)
