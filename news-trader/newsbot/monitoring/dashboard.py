"""Streamlit dashboard:  python -m newsbot dashboard   (or: streamlit run newsbot/monitoring/dashboard.py)

Read-only, except a button that turns the kill switch ON. There is deliberately no
"resume" button: resuming is done from the command line after reviewing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from newsbot.config import load_settings  # noqa: E402
from newsbot.monitoring import dashboard_data as data  # noqa: E402
from newsbot.risk.manager import RiskManager  # noqa: E402
from newsbot.storage.db import Database  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--config", default=str(Path(__file__).resolve().parents[2] / "config" / "settings.yaml"))
args, _ = parser.parse_known_args()

st.set_page_config(page_title="News Trader", page_icon="📰", layout="wide")


@st.cache_resource
def _open(config: str):
    settings = load_settings(config)
    return settings, Database(settings.database_url)


settings, db = _open(args.config)


@st.fragment(run_every="15s")
def page():
    risk = data.risk_usage(db, settings)
    mode = settings.mode.upper()
    st.title(f"📰 News Trader — {mode}")
    if settings.mode == "live":
        st.error("LIVE MODE: real money")
    if risk.killed:
        st.error(f"KILL SWITCH ON: {risk.kill_reason}. Resume from the command line: python -m newsbot resume")
    elif risk.halted_today:
        st.warning("Daily loss limit hit: no trading until tomorrow.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"{risk.equity:,.2f}" if risk.equity is not None else "—",
              f"{risk.day_change_pct:+.2%} today" if risk.day_change_pct is not None else None)
    c2.metric("Realised P&L", f"{risk.realized_pnl:+,.2f}")
    c3.metric("Trades today", f"{risk.trades_today} / {risk.max_trades}")
    c4.metric("Exposure", f"{risk.exposure:,.0f} / {risk.exposure_cap:,.0f}")

    st.subheader("Risk usage")
    r1, r2, r3 = st.columns(3)
    r1.caption("Open exposure vs. cap")
    r1.progress(min(risk.exposure / risk.exposure_cap, 1.0) if risk.exposure_cap else 0.0)
    r2.caption("Trades used today")
    r2.progress(min(risk.trades_today / risk.max_trades, 1.0))
    r3.caption(f"Daily loss vs. {risk.daily_loss_limit_pct:.0%} limit")
    loss = max(0.0, -(risk.day_change_pct or 0.0))
    r3.progress(min(loss / risk.daily_loss_limit_pct, 1.0))

    tabs = st.tabs(["Positions", "News & scores", "P&L", "Decisions"])
    with tabs[0]:
        _table(data.brackets(db, active=True), "No open positions.")
    with tabs[1]:
        _table(data.latest_news(db), "No news stored yet.")
    with tabs[2]:
        curve = data.equity_curve(db)
        if not curve.empty:
            st.line_chart(curve, y="equity")
        _table(data.brackets(db, active=False), "No closed trades yet.")
    with tabs[3]:
        _table(data.decisions(db), "No decisions logged yet.")


def _table(df, empty_message: str) -> None:
    if df.empty:
        st.info(empty_message)
    else:
        st.dataframe(df, hide_index=True)


page()

with st.sidebar:
    st.header("Kill switch")
    st.caption("Cancels all orders, closes all positions, stops trading until resumed from the command line.")
    confirm = st.checkbox("I want to stop all trading now")
    if st.button("Activate kill switch", type="primary", disabled=not confirm):
        RiskManager(settings.risk, settings.costs, db, settings.paths.kill_file).kill("dashboard button")
        st.success("Kill switch ON. The bot flattens within about a second.")
