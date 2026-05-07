"""Realistic Indian-market cost model — Zerodha-equivalent for delivery equity."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """All-in cost breakdown for an equity delivery trade in INR."""

    # Per-side brokerage. Zerodha charges ₹0 for delivery, but most brokers do.
    brokerage_pct: float = 0.0
    brokerage_flat_per_order: float = 0.0
    # Statutory: STT 0.1% on sell, exchange transaction 0.00297%, SEBI 0.0001%, GST 18% on (brok+exch)
    stt_sell_pct: float = 0.001
    exchange_pct: float = 0.0000297
    sebi_pct: float = 0.000001
    stamp_buy_pct: float = 0.00015  # buyer-side only
    gst_on_pct: float = 0.18  # applied to (brokerage + exchange)
    slippage_bps: float = 5.0  # 5 basis points = 0.05%

    def buy_cost(self, value: float) -> float:
        brok = self.brokerage_flat_per_order + value * self.brokerage_pct
        exch = value * self.exchange_pct
        sebi = value * self.sebi_pct
        stamp = value * self.stamp_buy_pct
        gst = (brok + exch) * self.gst_on_pct
        return brok + exch + sebi + stamp + gst

    def sell_cost(self, value: float) -> float:
        brok = self.brokerage_flat_per_order + value * self.brokerage_pct
        exch = value * self.exchange_pct
        sebi = value * self.sebi_pct
        stt = value * self.stt_sell_pct
        gst = (brok + exch) * self.gst_on_pct
        return brok + exch + sebi + stt + gst

    def slip_buy(self, price: float) -> float:
        return price * (1 + self.slippage_bps / 10_000)

    def slip_sell(self, price: float) -> float:
        return price * (1 - self.slippage_bps / 10_000)
