"""Test doubles: a scriptable price feed and a fake ccxt exchange."""

import ccxt

from newsbot.execution.sim import PriceFeed, PriceRange


class FakeFeed(PriceFeed):
    def __init__(self, prices=None):
        self.prices = dict(prices or {"BTC/USDT": 100.0, "ETH/USDT": 100.0})
        self.ranges = {}
        self.bar_data = {}

    def last_price(self, symbol):
        return self.prices[symbol]

    def bars(self, symbol, timeframe, limit):
        return self.bar_data.get(symbol, [])[-limit:]

    def range_since(self, symbol, since):
        r = self.ranges.pop(symbol, None)
        if r:
            return r
        p = self.prices[symbol]
        return PriceRange(p, p, p, p)

    def move(self, symbol, open_, high, low, last):
        self.ranges[symbol] = PriceRange(open_, high, low, last)
        self.prices[symbol] = last


class FakeExchange:
    """Enough of ccxt's spot API for the broker: limit/market orders, stop orders,
    Binance OCO lists, balances that lock while orders are open."""

    def __init__(self, exchange_id="binance", price=100.0, quote=10_000.0):
        self.id = exchange_id
        self.price = price
        self.balance = {"USDT": quote, "BTC": 0.0}
        self.orders = {}
        self.calls = []
        self._n = 0

    def _id(self):
        self._n += 1
        return str(self._n)

    def load_markets(self):
        return {}

    def market(self, symbol):
        return {"id": "BTCUSDT", "base": "BTC", "quote": "USDT",
                "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}}

    def parse_timeframe(self, tf):
        return ccxt.Exchange.parse_timeframe(tf)

    def amount_to_precision(self, symbol, amount):
        return f"{int(float(amount) * 1e6) / 1e6:.6f}"

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def fetch_ticker(self, symbol):
        return {"last": self.price}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        return []

    def _locked(self):
        quote = sum((o["amount"] - o["filled"]) * o["price"] for o in self.orders.values()
                    if o["status"] == "open" and o["side"] == "buy")
        lists = {}
        for o in self.orders.values():
            if o["status"] == "open" and o["side"] == "sell":
                lists[o.get("list") or o["id"]] = o["amount"] - o["filled"]
        return quote, sum(lists.values())

    def fetch_balance(self):
        lq, lb = self._locked()
        return {"free": {"USDT": self.balance["USDT"] - lq, "BTC": self.balance["BTC"] - lb},
                "total": dict(self.balance)}

    def _new(self, side, type_, amount, price, trigger=None, list_id=None):
        oid = self._id()
        self.orders[oid] = {"id": oid, "side": side, "type": type_, "amount": float(amount), "price": price,
                            "trigger": trigger, "status": "open", "filled": 0.0, "average": None, "list": list_id}
        return oid

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append((type_, side, float(amount), price))
        if type_ == "market":
            if side == "sell" and amount > self.fetch_balance()["free"]["BTC"] + 1e-9:
                raise ccxt.InsufficientFunds("coins locked")
            oid = self._new(side, type_, amount, self.price)
            self.fill(oid, price=self.price)
            return dict(self.orders[oid])
        return dict(self.orders[self._new(side, type_, amount, price)])

    def create_stop_loss_order(self, symbol, type_, side, amount, price=None, stopLossPrice=None, params=None):
        self.calls.append(("stop", side, float(amount), price, stopLossPrice))
        return dict(self.orders[self._new(side, "STOP_LOSS_LIMIT", amount, price, stopLossPrice)])

    def private_post_orderlist_oco(self, req):
        self.calls.append(("oco", dict(req)))
        lst = "L" + self._id()
        a = float(req["quantity"])
        s = self._new("sell", "STOP_LOSS_LIMIT", a, float(req["belowPrice"]), float(req["belowStopPrice"]), lst)
        t = self._new("sell", "LIMIT_MAKER", a, float(req["abovePrice"]), None, lst)
        return {"orderListId": lst, "orders": [{"orderId": int(s)}, {"orderId": int(t)}],
                "orderReports": [{"orderId": int(s), "type": "STOP_LOSS_LIMIT"},
                                 {"orderId": int(t), "type": "LIMIT_MAKER"}]}

    def fill(self, oid, qty=None, price=None):
        o = self.orders[oid]
        qty = qty or o["amount"]
        price = price or o["price"]
        o["filled"], o["average"] = qty, price
        o["fee"] = {"cost": qty * price * 0.001, "currency": "USDT"}
        if qty >= o["amount"]:
            o["status"] = "closed"
        sign = 1 if o["side"] == "buy" else -1
        self.balance["BTC"] += sign * qty
        self.balance["USDT"] -= sign * qty * price + qty * price * 0.001
        if o.get("list"):
            for other in self.orders.values():
                if other["list"] == o["list"] and other is not o and other["status"] == "open":
                    other["status"] = "expired"

    def fetch_order(self, oid, symbol):
        if oid not in self.orders:
            raise ccxt.OrderNotFound(oid)
        return dict(self.orders[oid])

    def cancel_order(self, oid, symbol):
        o = self.orders.get(oid)
        if not o or o["status"] != "open":
            raise ccxt.OrderNotFound(oid)
        for other in self.orders.values():
            if other is o or (o["list"] and other["list"] == o["list"]):
                other["status"] = "canceled"
        self.calls.append(("cancel", oid))

    def fetch_open_orders(self, symbol):
        return [dict(o) for o in self.orders.values() if o["status"] == "open"]

    def open_orders(self):
        return [o for o in self.orders.values() if o["status"] == "open"]
