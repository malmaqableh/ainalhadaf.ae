"""Bot state that survives restarts (open position, equity peak, halt flag, trade log)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Position:
    qty: float
    entry_price: float
    entry_fee: float
    stop_price: float
    take_profit_price: float | None
    opened_at: str
    stop_order_id: str | None = None  # stop-loss order resting on the exchange (testnet/live)
    tp_order_id: str | None = None    # take-profit leg of an OCO on the exchange


@dataclass
class BotState:
    initialized: bool = False
    cash: float = 0.0               # paper account only
    position: Position | None = None
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    last_candle_ts: int = 0
    halted: bool = False
    halt_reason: str = ""
    trades: list[dict] = field(default_factory=list)


MAX_TRADES_KEPT = 500


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        raw = json.loads(self.path.read_text())
        pos = raw.pop("position", None)
        state = BotState(**raw)
        state.position = Position(**pos) if pos else None
        return state

    def save(self, state: BotState) -> None:
        state.trades = state.trades[-MAX_TRADES_KEPT:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2))
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file
