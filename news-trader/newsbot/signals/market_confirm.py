"""Market confirmation: don't trade on news alone - price and volume must agree."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ConfirmationConfig


@dataclass
class Confirmation:
    ok: bool
    momentum: float
    volume_ratio: float
    reason: str


def bars_needed(cfg: ConfirmationConfig) -> int:
    return max(cfg.momentum_bars + 1, cfg.volume_lookback_bars + cfg.volume_recent_bars)


def confirm(bars: list[list], bullish: bool, cfg: ConfirmationConfig) -> Confirmation:
    """bars: closed candles [ts, open, high, low, close, volume], oldest first."""
    if len(bars) < bars_needed(cfg):
        return Confirmation(False, 0.0, 0.0, f"not enough price data ({len(bars)} of {bars_needed(cfg)} bars)")
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    momentum = closes[-1] / closes[-1 - cfg.momentum_bars] - 1
    recent = sum(vols[-cfg.volume_recent_bars:]) / cfg.volume_recent_bars
    base_window = vols[-(cfg.volume_recent_bars + cfg.volume_lookback_bars):-cfg.volume_recent_bars]
    base = sum(base_window) / len(base_window)
    ratio = recent / base if base > 0 else (float("inf") if recent > 0 else 0.0)
    signed = momentum if bullish else -momentum
    mom_ok = signed >= cfg.min_momentum_pct
    vol_ok = ratio >= cfg.min_volume_ratio
    problems = []
    if cfg.require_momentum and not mom_ok:
        problems.append(f"momentum {momentum:+.2%} (need {'+' if bullish else '-'}{cfg.min_momentum_pct:.2%})")
    if cfg.require_volume and not vol_ok:
        problems.append(f"volume {ratio:.2f}x normal (need {cfg.min_volume_ratio:.2f}x)")
    if problems:
        return Confirmation(False, momentum, ratio, "market not confirming: " + "; ".join(problems))
    return Confirmation(True, momentum, ratio, f"market confirms: momentum {momentum:+.2%}, volume {ratio:.2f}x")
