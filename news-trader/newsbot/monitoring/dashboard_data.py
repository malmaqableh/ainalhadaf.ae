"""Queries behind the dashboard (kept separate from Streamlit so they can be tested)."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from sqlalchemy import select

from ..config import Settings
from ..risk.manager import STATE_KEY
from ..storage.db import Bracket, Database, Decision, EquitySnapshot, Event, Impact


def latest_news(db: Database, limit: int = 50) -> pd.DataFrame:
    with db.session() as s:
        events = s.scalars(select(Event).order_by(Event.first_published_at.desc()).limit(limit)).all()
        ids = [e.id for e in events]
        impacts = s.scalars(select(Impact).where(Impact.event_id.in_(ids))).all() if ids else []
    by_event: dict[int, list[Impact]] = {}
    for i in impacts:
        by_event.setdefault(i.event_id, []).append(i)
    rows = []
    for e in events:
        imps = by_event.get(e.id, [])
        rows.append({
            "published (UTC)": e.first_published_at.strftime("%m-%d %H:%M"),
            "headline": e.headline,
            "sources": e.n_independent,
            "status": e.analysis_status,
            "impacts": "; ".join(f"{i.asset} {i.direction} {i.confidence:.2f} ({i.horizon})" for i in imps) or "-",
            "reason": " | ".join(i.reason for i in imps)[:200],
            "publishers": ", ".join(e.publishers),
        })
    return pd.DataFrame(rows)


def brackets(db: Database, active: bool) -> pd.DataFrame:
    with db.session() as s:
        q = select(Bracket)
        q = q.where(Bracket.status.in_(("pending", "open"))) if active else \
            q.where(Bracket.status == "closed").order_by(Bracket.closed_at.desc()).limit(200)
        rows = s.scalars(q).all()
    out = []
    for b in rows:
        row = {"symbol": b.symbol, "side": b.side, "status": b.status, "qty": round(b.qty, 6),
               "entry": b.entry_price or b.limit_price, "stop": b.stop_price, "target": b.take_profit_price}
        if active:
            row.update({"opened (UTC)": (b.filled_at or b.created_at).strftime("%m-%d %H:%M"),
                        "max hold until": b.max_hold_until.strftime("%m-%d %H:%M") if b.max_hold_until else "",
                        "why": b.reason})
        else:
            row.update({"exit": b.exit_price, "P&L": round(b.pnl or 0, 2), "exit reason": b.exit_reason,
                        "closed (UTC)": b.closed_at.strftime("%m-%d %H:%M") if b.closed_at else ""})
        out.append(row)
    return pd.DataFrame(out)


def equity_curve(db: Database, limit: int = 5000) -> pd.DataFrame:
    with db.session() as s:
        snaps = s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).limit(limit)).all()
    return pd.DataFrame([{"time": x.ts, "equity": x.equity} for x in reversed(snaps)]).set_index("time") \
        if snaps else pd.DataFrame(columns=["equity"])


def decisions(db: Database, limit: int = 300) -> pd.DataFrame:
    with db.session() as s:
        rows = s.scalars(select(Decision).order_by(Decision.ts.desc()).limit(limit)).all()
    return pd.DataFrame([{"time (UTC)": d.ts.strftime("%m-%d %H:%M:%S"), "stage": d.stage, "action": d.action,
                          "symbol": d.symbol, "reason": d.reason} for d in rows])


@dataclass
class RiskUsage:
    equity: float | None
    exposure: float
    exposure_cap: float
    trades_today: int
    max_trades: int
    day_change_pct: float | None
    daily_loss_limit_pct: float
    killed: bool
    kill_reason: str
    halted_today: bool
    realized_pnl: float


def risk_usage(db: Database, settings: Settings) -> RiskUsage:
    st = db.get_kv(STATE_KEY) or {}
    with db.session() as s:
        snap = s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).limit(1)).first()
        realized = sum(b.pnl or 0 for b in s.scalars(select(Bracket).where(Bracket.status == "closed")))
    equity = snap.equity if snap else None
    start = st.get("day_start_equity") or 0
    return RiskUsage(
        equity=equity, exposure=snap.exposure if snap else 0.0,
        exposure_cap=(equity or 0) * settings.risk.max_total_exposure_pct,
        trades_today=st.get("trades_today", 0), max_trades=settings.risk.max_trades_per_day,
        day_change_pct=(equity / start - 1) if equity and start else None,
        daily_loss_limit_pct=settings.risk.max_daily_loss_pct,
        killed=bool(st.get("killed")), kill_reason=st.get("kill_reason", ""),
        halted_today=bool(st.get("halted_day") and st.get("halted_day") == st.get("day")),
        realized_pnl=realized)
