"""Position sizing and the limits that stop the bot from losing too much."""

from __future__ import annotations

from .config import CostConfig, RiskConfig


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def position_size(self, equity: float, cash: float, price: float, costs: CostConfig) -> float:
        """Quantity to buy so that hitting the stop-loss loses about risk_per_trade of equity."""
        if price <= 0 or equity <= 0 or cash <= 0:
            return 0.0
        qty_by_risk = equity * self.cfg.risk_per_trade / (price * self.cfg.stop_loss_pct)
        qty_by_cap = equity * self.cfg.max_position_pct / price
        all_in_price = price * (1 + costs.slippage) * (1 + costs.fee_rate)
        qty_by_cash = cash / all_in_price
        return max(0.0, min(qty_by_risk, qty_by_cap, qty_by_cash))

    def stop_price(self, entry: float) -> float:
        return entry * (1 - self.cfg.stop_loss_pct)

    def take_profit_price(self, entry: float) -> float | None:
        if self.cfg.take_profit_pct <= 0:
            return None
        return entry * (1 + self.cfg.take_profit_pct)

    def daily_loss_hit(self, day_start_equity: float, equity: float) -> bool:
        return day_start_equity > 0 and equity <= day_start_equity * (1 - self.cfg.max_daily_loss_pct)

    def drawdown_hit(self, peak_equity: float, equity: float) -> bool:
        return peak_equity > 0 and equity <= peak_equity * (1 - self.cfg.max_drawdown_pct)
