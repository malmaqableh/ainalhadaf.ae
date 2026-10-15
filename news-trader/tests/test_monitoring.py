from datetime import timedelta

import httpx
import pytest

from newsbot.config import AlertsConfig
from newsbot.monitoring import dashboard_data as data
from newsbot.monitoring.alerts import Alerter
from newsbot.storage.db import Bracket, EquitySnapshot
from conftest import T0, Clock


def telegram_client(sent, status=200):
    def handler(req):
        sent.append(req)
        return httpx.Response(status, json={"ok": status == 200})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_telegram_alert(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent = []
    Alerter(AlertsConfig(telegram=True), http=telegram_client(sent)).send("trade", "FILLED long BTC")
    assert str(sent[0].url) == "https://api.telegram.org/bot123:abc/sendMessage"
    body = sent[0].read().decode()
    assert '"chat_id":"42"' in body.replace(" ", "") and "FILLED long BTC" in body and "PAPER" in body


def test_error_alerts_are_rate_limited_but_trades_are_not(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent, clock = [], Clock()
    a = Alerter(AlertsConfig(telegram=True, error_cooldown_minutes=15), http=telegram_client(sent), clock=clock)
    a.send("error", "feed down"); a.send("error", "feed down")
    a.send("trade", "x"); a.send("trade", "x")
    assert len(sent) == 3
    clock.t = T0 + timedelta(minutes=16)
    a.send("error", "feed down")
    assert len(sent) == 4


def test_alert_failures_never_raise(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def boom(req):
        raise httpx.ConnectError("no network")
    a = Alerter(AlertsConfig(telegram=True), http=httpx.Client(transport=httpx.MockTransport(boom)))
    a.send("trade", "still fine")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    a.send("trade", "missing token is logged, not raised")


def test_email_alert(monkeypatch):
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "bot@example.com",
                 "SMTP_PASSWORD": "pw", "ALERT_EMAIL_TO": "me@example.com"}.items():
        monkeypatch.setenv(k, v)
    log = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            log.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): log.append("tls")
        def login(self, u, p): log.append(("login", u))
        def send_message(self, m): log.append(("send", m["To"], m["Subject"]))

    Alerter(AlertsConfig(email=True), smtp_factory=FakeSMTP).send("risk", "DAILY LOSS LIMIT hit")
    assert log[0] == ("connect", "smtp.example.com", 587) and "tls" in log
    assert log[-1][0] == "send" and log[-1][1] == "me@example.com" and "risk" in log[-1][2]


# ---- dashboard queries --------------------------------------------------------------

def test_dashboard_data(db, settings):
    db.set_kv("risk_state", {"day": "2026-03-02", "day_start_equity": 10_000, "trades_today": 2,
                             "halted_day": None, "killed": False, "kill_reason": ""})
    db.save(EquitySnapshot(ts=T0, equity=9_900, exposure=1_500))
    with db.session() as s:
        s.add(Bracket(symbol="BTC/USDT", side="long", status="open", qty=1, limit_price=100, entry_price=100,
                      stop_price=98, take_profit_price=104, entry_deadline=T0, created_at=T0, filled_at=T0,
                      max_hold_until=T0 + timedelta(hours=8), reason="ETF news"))
        s.add(Bracket(symbol="ETH/USDT", side="long", status="closed", qty=1, limit_price=100, entry_price=100,
                      stop_price=98, take_profit_price=104, entry_deadline=T0, exit_price=104, pnl=3.8,
                      exit_reason="take-profit", closed_at=T0))
        s.commit()
    r = data.risk_usage(db, settings)
    assert r.equity == 9_900 and r.exposure_cap == 9_900 * 0.2 and r.trades_today == 2
    assert r.day_change_pct == pytest.approx(-0.01) and r.realized_pnl == 3.8 and not r.killed
    assert list(data.brackets(db, active=True)["symbol"]) == ["BTC/USDT"]
    assert list(data.brackets(db, active=False)["P&L"]) == [3.8]
    assert len(data.equity_curve(db)) == 1
    db.log_decision("risk", "approved", "ok", "BTC/USDT")
    assert data.decisions(db).iloc[0]["action"] == "approved"
    assert data.latest_news(db).empty
