import sys
from pathlib import Path

import ccxt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HOUR_MS = 3_600_000
BASE_TS = 1_700_006_400_000  # 2023-11-15 00:00 UTC, on an hour boundary


def make_candles(closes, start_ts=BASE_TS, step_ms=HOUR_MS):
    """Candles whose open is the previous close; high/low hug the bar."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append([start_ts + i * step_ms, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 1.0])
        prev = c
    return out


class FakeExchange:
    """Stands in for a ccxt exchange: serves fixed candles and a settable price."""

    def __init__(self, candles, price=None):
        self.candles = candles
        self.price = price if price is not None else candles[-1][4]

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        rows = self.candles
        if since is not None:
            rows = [c for c in rows if c[0] >= since]
            return rows[:limit]
        return rows[-limit:] if limit else rows

    def fetch_ticker(self, symbol):
        return {"last": self.price}


class FakeLiveExchange(FakeExchange):
    """An authenticated exchange: balances, market orders, stop orders and Binance-style OCO.
    Open orders lock their coins (once per OCO, like Binance); filling one OCO leg expires
    the other, and cancelling one leg cancels both."""

    def __init__(self, candles, price=None, quote=1000.0, exchange_id="test"):
        super().__init__(candles, price)
        self.id = exchange_id
        self.balance = {"USDT": quote, "BTC": 0.0}
        self.orders = {}
        self.calls = []
        self.stop_error = None
        self.oco_error = None
        self.oco_response_override = None
        self._next_id = 0

    def _id(self):
        self._next_id += 1
        return str(self._next_id)

    def load_markets(self):
        return {}

    def market(self, symbol):
        return {"id": "BTCUSDT", "base": "BTC", "quote": "USDT", "spot": True,
                "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}}

    def amount_to_precision(self, symbol, amount):
        return f"{int(float(amount) * 1e6) / 1e6:.6f}"

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def fetch_balance(self):
        locked = {}
        for o in self.orders.values():
            if o["status"] == "open":
                locked[o.get("list") or o["id"]] = o["amount"] - o["filled"]
        free_btc = self.balance["BTC"] - sum(locked.values())
        return {"free": {"USDT": self.balance["USDT"], "BTC": free_btc}, "total": dict(self.balance)}

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.calls.append(("market", side, amount))
        if side == "sell" and amount > self.fetch_balance()["free"]["BTC"] + 1e-12:
            raise RuntimeError("insufficient balance (coins locked by an open order?)")
        sign = 1 if side == "buy" else -1
        self.balance["BTC"] += sign * amount
        self.balance["USDT"] -= sign * amount * self.price
        return {"id": self._id(), "status": "closed", "filled": amount, "average": self.price}

    def _new_order(self, type, amount, price, trigger, list_id=None):
        oid = self._id()
        self.orders[oid] = {"id": oid, "type": type, "amount": amount, "price": price, "trigger": trigger,
                            "status": "open", "filled": 0.0, "average": None, "list": list_id}
        return oid

    def create_stop_loss_order(self, symbol, type, side, amount, price=None, stopLossPrice=None, params=None):
        if self.stop_error:
            raise self.stop_error
        oid = self._new_order(type, amount, price, stopLossPrice)
        self.calls.append(("stop", type, amount, price, stopLossPrice))
        return dict(self.orders[oid])

    def private_post_orderlist_oco(self, request):
        if self.oco_error:
            raise self.oco_error
        self.calls.append(("oco", dict(request)))
        amount = float(request["quantity"])
        list_id = "L" + self._id()
        below_price = float(request["belowPrice"]) if "belowPrice" in request else None
        stop_id = self._new_order(request["belowType"], amount, below_price,
                                  float(request["belowStopPrice"]), list_id)
        tp_id = self._new_order("LIMIT_MAKER", amount, float(request["abovePrice"]), None, list_id)
        response = {"orderListId": list_id, "contingencyType": "OCO",
                    "orders": [{"orderId": int(stop_id)}, {"orderId": int(tp_id)}],
                    "orderReports": [{"orderId": int(stop_id), "type": request["belowType"]},
                                     {"orderId": int(tp_id), "type": "LIMIT_MAKER"}]}
        return self.oco_response_override(response) if self.oco_response_override else response

    def fill_order(self, oid, qty=None):
        """Simulate the exchange executing (part of) an order."""
        o = self.orders[oid]
        qty = qty or o["amount"]
        o["filled"] = qty
        o["average"] = o["price"] or o["trigger"]
        if qty >= o["amount"]:
            o["status"] = "closed"
        if o["list"]:
            for other in self.orders.values():
                if other["list"] == o["list"] and other is not o and other["status"] == "open":
                    other["status"] = "expired"
        self.balance["BTC"] -= qty
        self.balance["USDT"] += qty * o["average"]

    fill_stop = fill_order

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
