"""Telegram and email alerts. Sending failures are logged, never raised."""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Callable

import httpx

from ..config import AlertsConfig
from ..storage.db import utcnow

log = logging.getLogger(__name__)

ICONS = {"trade": "💹", "error": "⚠️", "risk": "🛑", "info": "ℹ️"}


class Alerter:
    def __init__(self, cfg: AlertsConfig, mode: str = "paper", http: httpx.Client | None = None,
                 clock: Callable[[], datetime] = utcnow, smtp_factory=smtplib.SMTP):
        self.cfg = cfg
        self.mode = mode
        self.http = http or httpx.Client(timeout=10)
        self.clock = clock
        self.smtp_factory = smtp_factory
        self._last_error: dict[str, datetime] = {}

    def send(self, kind: str, message: str) -> None:
        """kind: trade | error | risk | info"""
        if kind == "error":
            key = message[:120]
            last = self._last_error.get(key)
            now = self.clock()
            if last and now - last < timedelta(minutes=self.cfg.error_cooldown_minutes):
                return
            self._last_error[key] = now
        text = f"{ICONS.get(kind, '')} [newsbot {self.mode.upper()}] {message}"
        log.info("alert (%s): %s", kind, message)
        if self.cfg.telegram:
            self._telegram(text)
        if self.cfg.email:
            self._email(kind, text)

    def _telegram(self, text: str) -> None:
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            log.error("telegram alerts on but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
            return
        try:
            r = self.http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                               json={"chat_id": chat, "text": text[:4000]})
            if r.status_code != 200:
                log.error("telegram alert failed: HTTP %s %s", r.status_code, r.text[:200])
        except Exception as e:
            log.error("telegram alert failed: %s", e)

    def _email(self, kind: str, text: str) -> None:
        host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_EMAIL_TO")
        if not host or not to:
            log.error("email alerts on but SMTP_HOST / ALERT_EMAIL_TO not set")
            return
        msg = EmailMessage()
        msg["Subject"] = f"newsbot {self.mode} {kind}: {text[:80]}"
        msg["From"] = os.environ.get("SMTP_USER") or to
        msg["To"] = to
        msg.set_content(text)
        try:
            with self.smtp_factory(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
                smtp.starttls()
                if os.environ.get("SMTP_USER"):
                    smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                smtp.send_message(msg)
        except Exception as e:
            log.error("email alert failed: %s", e)
