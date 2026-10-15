"""Simulated order fills, shared by the paper broker and the backtester."""

from __future__ import annotations

from dataclasses import dataclass

from .config import CostConfig


@dataclass
class Fill:
    side: str
    qty: float
    price: float
    fee: float  # in quote currency


def simulate_buy(qty: float, ref_price: float, cash: float, costs: CostConfig) -> Fill | None:
    price = ref_price * (1 + costs.slippage)
    qty = min(qty, cash / (price * (1 + costs.fee_rate)))
    if qty <= 0:
        return None
    return Fill("buy", qty, price, qty * price * costs.fee_rate)


def simulate_sell(qty: float, ref_price: float, costs: CostConfig) -> Fill | None:
    if qty <= 0:
        return None
    price = ref_price * (1 - costs.slippage)
    return Fill("sell", qty, price, qty * price * costs.fee_rate)
