"""The 24/7 loop: risk checks, position management, news, analysis, signals, orders."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .analysis.base import Analyzer
from .analysis.service import AnalysisService
from .config import AssetsConfig, Settings, SourcesConfig
from .execution.broker import Broker
from .execution.executor import Executor
from .ingestion.poller import Ingestor
from .monitoring.alerts import Alerter
from .risk.manager import RiskAction, RiskManager
from .signals.generator import SignalGenerator
from .storage.db import Database, EquitySnapshot, utcnow

log = logging.getLogger(__name__)

SNAPSHOT_EVERY = timedelta(minutes=1)
MAX_BACKOFF_SECONDS = 300


class NewsBot:
    def __init__(self, settings: Settings, sources: SourcesConfig, assets: AssetsConfig, db: Database,
                 broker: Broker, analyzer: Analyzer, alerter: Alerter, ingestor: Ingestor | None = None,
                 clock: Callable[[], datetime] = utcnow, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self.assets = assets
        self.db = db
        self.broker = broker
        self.alerter = alerter
        self.clock = clock
        self.sleep = sleep
        self.symbols = [a.symbol for a in assets.assets]
        self.risk = RiskManager(settings.risk, settings.costs, db, settings.paths.kill_file,
                                entry_timeout_seconds=settings.signals.entry_timeout_seconds,
                                entry_offset_pct=settings.signals.entry_limit_offset_pct,
                                broker_supports_short=broker.supports_short, clock=clock)
        self.executor = Executor(db, broker, self.risk, notify=alerter.send, clock=clock)
        on_error = lambda msg: alerter.send("error", msg)  # noqa: E731
        self.ingestor = ingestor or Ingestor(db, settings, sources, clock=clock, on_error=on_error)
        self.analysis = AnalysisService(db, settings, assets, analyzer, clock=clock, on_error=on_error)
        self.signals = SignalGenerator(db, settings, sources, clock=clock)
        self._last_snapshot: datetime | None = None
        self._last_block: str | None = None

    def tick(self) -> None:
        now = self.clock()
        account = self.broker.account()

        # 1. Risk first: kill switch and daily loss limit can flatten everything.
        for action in self.risk.update(account):
            if action == RiskAction.KILL_HIT:
                self.alerter.send("risk", f"KILL SWITCH: {self.risk.state()['kill_reason']}. "
                                          "Cancelling all orders and closing all positions.")
                self.db.log_decision("risk", "kill_switch", self.risk.state()["kill_reason"])
            elif action == RiskAction.DAILY_LOSS_HIT:
                self.alerter.send("risk", f"DAILY LOSS LIMIT hit (equity {account.equity:,.2f}). "
                                          "Closing all positions; no trading until tomorrow.")
                self.db.log_decision("risk", "daily_loss_limit", f"equity {account.equity:.2f}")
            elif action in (RiskAction.FLATTEN_KILL, RiskAction.FLATTEN_DAILY_LOSS):
                if self.db.active_brackets():
                    reason = "kill switch" if action == RiskAction.FLATTEN_KILL else "daily loss limit"
                    self.executor.flatten_all(reason, self.symbols)

        # 2. Positions: fills, stops, targets, timeouts.
        self.executor.manage()

        # 3. News in, analysis out (each source runs on its own schedule).
        self.ingestor.poll_due()
        self.analysis.process_pending()

        # 4. Signals -> risk gate -> orders. While trading is blocked (kill switch, daily loss,
        #    outside the session) no new signals are acted on.
        blocked = self.risk.block_reason()
        if blocked != self._last_block:
            self.db.log_decision("risk", "trading_blocked" if blocked else "trading_allowed",
                                 blocked or "no risk block in force")
            self._last_block = blocked
        if blocked:
            return self._snapshot(now, account)
        held_long = {b.symbol for b in self.db.active_brackets() if b.side == "long" and b.status == "open"}
        output = self.signals.evaluate(self.broker, held_long)
        for close in output.closes:
            self.executor.close_symbol(close.symbol, close.reason)
        for intent in output.intents:
            order, reason = self.risk.approve(intent, self.broker.account(), self.db.active_brackets(),
                                              self.broker.last_price(intent.symbol))
            self.db.log_decision("risk", "approved" if order else "rejected", reason, intent.symbol,
                                 {"intent": intent.reason})
            if order:
                self.executor.open(order)
        self._snapshot(now, account)

    def _snapshot(self, now: datetime, account) -> None:
        """Equity history for the dashboard."""
        if self._last_snapshot is None or now - self._last_snapshot >= SNAPSHOT_EVERY:
            exposure = sum(b.qty * (b.entry_price or b.limit_price) for b in self.db.active_brackets())
            self.db.save(EquitySnapshot(ts=now, equity=account.equity, exposure=exposure))
            self._last_snapshot = now

    def _sleep_watching_kill_file(self, seconds: float) -> None:
        """Sleep, but wake up within a second if the kill file appears."""
        kill = Path(self.settings.paths.kill_file)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if kill.exists() and not self.risk.state()["killed"]:
                return
            self.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def run_forever(self) -> None:
        log.info("newsbot started in %s mode (%s, %d assets)", self.settings.mode.upper(),
                 self.broker.name, len(self.symbols))
        self.alerter.send("info", f"Started in {self.settings.mode.upper()} mode.")
        errors = 0
        try:
            while True:
                try:
                    self.tick()
                    errors = 0
                    delay = self.settings.tick_seconds
                except Exception as e:
                    errors += 1
                    delay = min(self.settings.tick_seconds * 2 ** errors, MAX_BACKOFF_SECONDS)
                    log.exception("tick failed (%d in a row); retrying in %ss", errors, delay)
                    self.alerter.send("error", f"Loop error ({errors} in a row): {e}")
                self._sleep_watching_kill_file(delay)
        except KeyboardInterrupt:
            log.info("stopped by user (open positions keep their protective orders / stay in the paper account)")
