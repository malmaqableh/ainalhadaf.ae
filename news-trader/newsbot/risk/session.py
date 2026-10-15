"""Market session window: no trading outside the session or in its first/last minutes."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import SessionConfig


def session_block_reason(now: datetime, cfg: SessionConfig) -> str | None:
    """None when trading is allowed now, else why not. Exchange holidays are not modelled."""
    if not cfg.enabled:
        return None
    local = now.astimezone(ZoneInfo(cfg.timezone))
    if local.weekday() not in cfg.weekdays:
        return "market closed (not a trading day)"
    oh, om = map(int, cfg.open.split(":"))
    ch, cm = map(int, cfg.close.split(":"))
    open_at = local.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_at = local.replace(hour=ch, minute=cm, second=0, microsecond=0)
    if local < open_at or local >= close_at:
        return "market closed"
    if local < open_at + timedelta(minutes=cfg.no_trade_open_minutes):
        return f"first {cfg.no_trade_open_minutes} minutes of the session"
    if local >= close_at - timedelta(minutes=cfg.no_trade_close_minutes):
        return f"last {cfg.no_trade_close_minutes} minutes of the session"
    return None
