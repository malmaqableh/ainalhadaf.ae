"""Builds the broker for the configured mode. Live trading needs two deliberate steps."""

from __future__ import annotations

import os

import ccxt

from ..config import AssetsConfig, Settings
from .broker import Broker
from .ccxt_broker import CcxtFeed, CcxtSpotBroker
from .sim import SimulatedBroker


class LiveTradingLocked(RuntimeError):
    pass


def make_exchange(settings: Settings, authenticated: bool):
    cls = getattr(ccxt, settings.exchange, None)
    if cls is None:
        raise ValueError(f"unknown exchange {settings.exchange!r}")
    params: dict = {"enableRateLimit": True}
    if authenticated:
        key, secret = os.environ.get("EXCHANGE_API_KEY"), os.environ.get("EXCHANGE_API_SECRET")
        if not key or not secret:
            raise LiveTradingLocked("EXCHANGE_API_KEY and EXCHANGE_API_SECRET must be set in .env")
        params.update(apiKey=key, secret=secret)
    exchange = cls(params)
    if settings.mode == "testnet":
        exchange.set_sandbox_mode(True)
    return exchange


def make_broker(settings: Settings, assets: AssetsConfig) -> Broker:
    if settings.mode == "paper":
        feed = CcxtFeed(make_exchange(settings, authenticated=False))
        return SimulatedBroker(feed, settings.costs, settings.risk.starting_cash,
                               supports_short=settings.risk.allow_short, state_path=settings.paths.paper_state)
    if settings.mode == "live" and os.environ.get("NEWSBOT_CONFIRM_LIVE") != "YES":
        raise LiveTradingLocked("mode is 'live' but NEWSBOT_CONFIRM_LIVE=YES is not set in .env. "
                                "Real money is at risk: run paper and testnet first.")
    if settings.mode not in ("testnet", "live"):
        raise LiveTradingLocked(f"unknown mode {settings.mode!r}")
    return CcxtSpotBroker(make_exchange(settings, authenticated=True), settings.quote_currency,
                          [a.symbol for a in assets.assets])
