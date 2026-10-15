"""Download historical candles and save/load them as CSV."""

from __future__ import annotations

import csv
import time
from pathlib import Path

import ccxt

HEADER = ["timestamp", "open", "high", "low", "close", "volume"]


def fetch_history(exchange, symbol: str, timeframe: str, since_ms: int,
                  until_ms: int | None = None, page_limit: int = 1000) -> list[list]:
    """Closed candles from since_ms up to until_ms (default: now), following pagination."""
    tf_ms = ccxt.Exchange.parse_timeframe(timeframe) * 1000
    until_ms = until_ms or int(time.time() * 1000)
    out: list[list] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=page_limit)
        added = 0
        for c in batch:
            if (not out or c[0] > out[-1][0]) and c[0] + tf_ms <= until_ms:
                out.append(c)
                added += 1
        if not added:
            break
        cursor = out[-1][0] + tf_ms
    return out


def save_csv(candles: list[list], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(candles)


def load_csv(path: str | Path) -> list[list]:
    with Path(path).open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        if [h.strip().lower() for h in header[:6]] != HEADER:
            raise ValueError(f"CSV header must be {','.join(HEADER)}")
        return [[int(float(r[0]))] + [float(x) for x in r[1:6]] for r in reader if r]
