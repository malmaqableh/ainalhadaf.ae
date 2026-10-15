import sys
from pathlib import Path

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
