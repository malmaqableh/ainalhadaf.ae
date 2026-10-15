"""The risk gate. Nothing reaches the broker without passing through here.

Signal logic can only produce an OrderIntent. Only RiskManager.approve() creates
an ApprovedOrder, and the executor only submits an order that RiskManager.verify()
recognises as one it issued, unchanged, still valid, and not yet used.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from ..config import CostConfig, RiskConfig
from ..storage.db import Bracket, Database, utcnow
from .session import session_block_reason

log = logging.getLogger(__name__)

STATE_KEY = "risk_state"
_TOKEN = object()  # only this module can mint ApprovedOrders


@dataclass(frozen=True)
class OrderIntent:
    """What the signal logic would like to do. Carries no size: risk decides that."""
    symbol: str
    side: str            # long | short
    horizon: str         # minutes | hours | days
    confidence: float
    reason: str
    event_id: int | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float          # free quote currency


@dataclass(frozen=True)
class ApprovedOrder:
    serial: int
    symbol: str
    side: str
    qty: float
    limit_price: float
    stop_price: float
    take_profit_price: float
    horizon: str
    max_hold_until: datetime
    entry_deadline: datetime
    confidence: float
    reason: str
    event_id: int | None
    _token: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._token is not _TOKEN:
            raise PermissionError("ApprovedOrder can only be created by RiskManager.approve()")

    @property
    def notional(self) -> float:
        return self.qty * self.limit_price


class RiskAction(str, Enum):
    # Repeated on every tick while in force, so a flatten that failed is retried.
    FLATTEN_DAILY_LOSS = "flatten_daily_loss"
    FLATTEN_KILL = "flatten_kill"
    # Emitted once, when the condition starts (for alerts).
    DAILY_LOSS_HIT = "daily_loss_hit"
    KILL_HIT = "kill_hit"


class RiskManager:
    def __init__(self, cfg: RiskConfig, costs: CostConfig, db: Database, kill_file: str,
                 entry_timeout_seconds: int = 120, entry_offset_pct: float = 0.0005,
                 broker_supports_short: bool = False, clock: Callable[[], datetime] = utcnow):
        self.cfg = cfg
        self.costs = costs
        self.db = db
        self.kill_file = Path(kill_file)
        self.entry_timeout = timedelta(seconds=entry_timeout_seconds)
        self.entry_offset_pct = entry_offset_pct
        self.broker_supports_short = broker_supports_short
        self.clock = clock
        self._serials = itertools.count(1)
        self._issued: dict[int, ApprovedOrder] = {}

    # ---- persistent state ----------------------------------------------

    def state(self) -> dict:
        return self.db.get_kv(STATE_KEY, None) or {
            "day": None, "day_start_equity": 0.0, "trades_today": 0,
            "halted_day": None, "killed": False, "kill_reason": ""}

    def _save(self, st: dict) -> None:
        self.db.set_kv(STATE_KEY, st)

    def _today(self) -> str:
        return self.clock().astimezone(ZoneInfo(self.cfg.day_timezone)).date().isoformat()

    # ---- called every tick ---------------------------------------------

    def update(self, account: AccountSnapshot) -> list[RiskAction]:
        """Roll the trading day, watch the daily loss limit and the kill switch."""
        st = self.state()
        actions: list[RiskAction] = []
        today = self._today()
        if st["day"] != today:
            st.update(day=today, day_start_equity=account.equity, trades_today=0)
            if st["halted_day"] and st["halted_day"] != today:
                log.info("new trading day: daily-loss halt lifted")
                st["halted_day"] = None
        if self.kill_file.exists() and not st["killed"]:
            st.update(killed=True, kill_reason=f"kill file {self.kill_file} found")
        if st["killed"] and not st.get("kill_alerted"):
            st["kill_alerted"] = True
            actions.append(RiskAction.KILL_HIT)
        if st["killed"]:
            actions.append(RiskAction.FLATTEN_KILL)
        start = st["day_start_equity"]
        if (st["halted_day"] != today and start > 0
                and account.equity <= start * (1 - self.cfg.max_daily_loss_pct)):
            st["halted_day"] = today
            log.error("daily loss limit hit: equity %.2f vs %.2f at day start", account.equity, start)
            actions.append(RiskAction.DAILY_LOSS_HIT)
        if st["halted_day"] == today:
            actions.append(RiskAction.FLATTEN_DAILY_LOSS)
        self._save(st)
        return actions

    def kill(self, reason: str) -> None:
        st = self.state()
        st.update(killed=True, kill_reason=reason)
        self._save(st)
        self.kill_file.parent.mkdir(parents=True, exist_ok=True)
        self.kill_file.touch()

    def resume(self) -> None:
        """Clear the kill switch (a human decision). Daily-loss halts lift on their own next day."""
        st = self.state()
        st.update(killed=False, kill_reason="", kill_alerted=False)
        self._save(st)
        self.kill_file.unlink(missing_ok=True)

    def block_reason(self) -> str | None:
        """Why no new trades may be opened right now (None = allowed)."""
        st = self.state()
        if st["killed"] or self.kill_file.exists():
            return f"kill switch active ({st['kill_reason'] or 'kill file present'})"
        if st["halted_day"] == self._today():
            return "daily loss limit hit; no trading until the next day"
        return session_block_reason(self.clock(), self.cfg.session)

    # ---- the gate --------------------------------------------------------

    def approve(self, intent: OrderIntent, account: AccountSnapshot, active: list[Bracket],
                last_price: float) -> tuple[ApprovedOrder | None, str]:
        """Return (order, reason). order is None when the intent is rejected."""
        cfg = self.cfg
        if (reason := self.block_reason()):
            return None, reason
        st = self.state()
        if st["day"] != self._today():
            return None, "risk state not initialised for today (update() must run first)"
        if intent.side not in ("long", "short"):
            return None, f"invalid side {intent.side!r}"
        if intent.side == "short" and not (cfg.allow_short and self.broker_supports_short):
            return None, "short selling is disabled"
        if intent.horizon not in cfg.horizons:
            return None, f"unknown horizon {intent.horizon!r}"
        if st["trades_today"] >= cfg.max_trades_per_day:
            return None, f"max trades per day reached ({cfg.max_trades_per_day})"
        if any(b.symbol == intent.symbol and b.is_active for b in active):
            return None, f"already have a position or order in {intent.symbol}"
        if account.equity <= 0 or last_price <= 0:
            return None, "no equity or no valid price"

        h = cfg.horizons[intent.horizon]
        sign = 1 if intent.side == "long" else -1
        limit = last_price * (1 + sign * self.entry_offset_pct)
        stop = limit * (1 - sign * h.stop_loss_pct)
        take_profit = limit * (1 + sign * h.take_profit_pct)

        # Loss per unit if the stop is hit, including fees on both legs and slippage on the stop.
        per_unit_loss = abs(limit - stop) + limit * self.costs.fee_rate + stop * (self.costs.fee_rate + self.costs.slippage)
        qty_by_risk = account.equity * cfg.max_risk_per_trade_pct / per_unit_loss
        exposure = sum(b.qty * (b.entry_price or b.limit_price) for b in active if b.is_active)
        room = account.equity * cfg.max_total_exposure_pct - exposure
        qty_by_exposure = max(0.0, room) / limit
        qty_by_cash = account.cash / (limit * (1 + self.costs.fee_rate)) if intent.side == "long" else qty_by_exposure
        qty = min(qty_by_risk, qty_by_exposure, qty_by_cash)
        if qty * limit < cfg.min_order_notional:
            if qty_by_exposure * limit < cfg.min_order_notional:
                return None, (f"total exposure limit reached ({exposure:.2f} of "
                              f"{account.equity * cfg.max_total_exposure_pct:.2f})")
            return None, f"position would be below the minimum order size ({qty * limit:.2f})"

        now = self.clock()
        order = ApprovedOrder(
            serial=next(self._serials), symbol=intent.symbol, side=intent.side, qty=qty,
            limit_price=limit, stop_price=stop, take_profit_price=take_profit, horizon=intent.horizon,
            max_hold_until=now + timedelta(minutes=h.max_hold_minutes),
            entry_deadline=now + self.entry_timeout, confidence=intent.confidence,
            reason=intent.reason, event_id=intent.event_id, _token=_TOKEN)
        self._issued[order.serial] = order
        st["trades_today"] += 1
        self._save(st)
        return order, (f"approved {intent.side} {qty:.6f} {intent.symbol} @ {limit:.4f}, "
                       f"stop {stop:.4f}, target {take_profit:.4f}, risk "
                       f"{qty * per_unit_loss:.2f} ({qty * per_unit_loss / account.equity:.2%} of equity)")

    def verify(self, order: ApprovedOrder) -> str | None:
        """Called by the executor right before submitting. One use only."""
        issued = self._issued.pop(getattr(order, "serial", None), None)
        if issued is None or issued is not order:
            return "order was not issued by the risk manager (or was modified or already used)"
        if (reason := self.block_reason()):
            return reason
        if self.clock() > order.entry_deadline:
            return "approval expired"
        return None
