"""Baseline strategies. Each consumes a single-symbol date-indexed frame
(with required indicators already computed) and emits entry/exit/stop columns."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass
class StrategySignals:
    """Per-bar signals for one symbol."""

    entry: pd.Series  # bool
    exit: pd.Series  # bool
    stop_price: pd.Series  # float; NaN if no stop


class Strategy(ABC):
    """Long-only daily-bar strategy. signals() must not look ahead."""

    name: str = "abstract"
    indicators: tuple[str, ...] = ()

    @abstractmethod
    def signals(self, df: pd.DataFrame) -> StrategySignals: ...


class EmaCrossover(Strategy):
    """Entry: fast EMA crosses ABOVE slow EMA. Exit: fast crosses BELOW slow.
    Stop: entry close - atr_mult * ATR."""

    name = "ema_crossover"

    def __init__(self, fast: int = 20, slow: int = 50, atr_mult: float = 2.0):
        self.fast = fast
        self.slow = slow
        self.atr_mult = atr_mult
        self.indicators = (f"ema{fast}", f"ema{slow}", "atr14")

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        f = df[f"ema{self.fast}"]
        s = df[f"ema{self.slow}"]
        prev_below = (f.shift(1) <= s.shift(1))
        now_above = (f > s)
        entry = (prev_below & now_above).fillna(False)
        prev_above = (f.shift(1) >= s.shift(1))
        now_below = (f < s)
        exit_ = (prev_above & now_below).fillna(False)
        stop = df["close"] - self.atr_mult * df["atr14"]
        return StrategySignals(entry=entry, exit=exit_, stop_price=stop)


class RsiMeanReversion(Strategy):
    """Entry: RSI crosses BELOW oversold (default 30). Exit: RSI crosses ABOVE overbought (default 60).
    Stop: entry close - atr_mult * ATR (loose, since strategy expects whipsaws)."""

    name = "rsi_mean_reversion"

    def __init__(self, oversold: int = 30, overbought: int = 60, atr_mult: float = 3.0):
        self.oversold = oversold
        self.overbought = overbought
        self.atr_mult = atr_mult
        self.indicators = ("rsi14", "atr14")

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        r = df["rsi14"]
        entry = ((r.shift(1) >= self.oversold) & (r < self.oversold)).fillna(False)
        exit_ = ((r.shift(1) <= self.overbought) & (r > self.overbought)).fillna(False)
        stop = df["close"] - self.atr_mult * df["atr14"]
        return StrategySignals(entry=entry, exit=exit_, stop_price=stop)


class BollingerBreakout(Strategy):
    """Entry: close breaks ABOVE upper Bollinger band on volume > vol_mult × 20-day avg.
    Exit: close drops BELOW middle band. Stop: entry close - atr_mult * ATR."""

    name = "bollinger_breakout"

    def __init__(self, vol_mult: float = 2.0, atr_mult: float = 2.0):
        self.vol_mult = vol_mult
        self.atr_mult = atr_mult
        self.indicators = ("bbands", "atr14", "vol_sma20")

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        breakout = (df["close"] > df["bb_upper"]) & (df["close"].shift(1) <= df["bb_upper"].shift(1))
        vol_ok = df["volume"] > self.vol_mult * df["vol_sma20"]
        entry = (breakout & vol_ok).fillna(False)
        exit_ = (df["close"] < df["bb_mid"]).fillna(False)
        stop = df["close"] - self.atr_mult * df["atr14"]
        return StrategySignals(entry=entry, exit=exit_, stop_price=stop)


def _trend_up(df: pd.DataFrame) -> pd.Series:
    """Long-only sanity gate: above the 200-day SMA."""
    return (df["close"] > df["sma200"]).fillna(False)


def _trending_market(df: pd.DataFrame, min_adx: float = 25.0) -> pd.Series:
    """ADX > threshold = directional regime, not chop."""
    return (df["adx14"] > min_adx).fillna(False)


def _vol_ok(df: pd.DataFrame, max_atr_pct: float = 0.05) -> pd.Series:
    """Skip names where 14d ATR exceeds 5% of price (overheated / news-driven)."""
    return ((df["atr14"] / df["close"]) < max_atr_pct).fillna(False)


class EmaCrossoverFiltered(EmaCrossover):
    """EMA crossover with trend + ADX + volatility filters. Cuts noise signals."""

    name = "ema_crossover_filtered"

    def __init__(self, fast: int = 20, slow: int = 50, atr_mult: float = 2.0):
        super().__init__(fast=fast, slow=slow, atr_mult=atr_mult)
        self.indicators = (f"ema{fast}", f"ema{slow}", "atr14", "sma200", "adx14")

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        base = super().signals(df)
        gate = _trend_up(df) & _trending_market(df) & _vol_ok(df)
        return StrategySignals(entry=base.entry & gate, exit=base.exit, stop_price=base.stop_price)


class RsiMeanReversionFiltered(RsiMeanReversion):
    """RSI mean-reversion only in established uptrends (catch pullbacks, not crashes)."""

    name = "rsi_mean_reversion_filtered"

    def __init__(self, oversold: int = 30, overbought: int = 60, atr_mult: float = 3.0):
        super().__init__(oversold=oversold, overbought=overbought, atr_mult=atr_mult)
        self.indicators = ("rsi14", "atr14", "sma200")

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        base = super().signals(df)
        gate = _trend_up(df) & _vol_ok(df)
        return StrategySignals(entry=base.entry & gate, exit=base.exit, stop_price=base.stop_price)


class BollingerBreakoutFiltered(BollingerBreakout):
    """Bollinger breakout only in trending markets (skip range-bound false breakouts)."""

    name = "bollinger_breakout_filtered"

    def __init__(self, vol_mult: float = 2.0, atr_mult: float = 2.0):
        super().__init__(vol_mult=vol_mult, atr_mult=atr_mult)
        self.indicators = ("bbands", "atr14", "vol_sma20", "sma200", "adx14")

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        base = super().signals(df)
        gate = _trend_up(df) & _trending_market(df)
        return StrategySignals(entry=base.entry & gate, exit=base.exit, stop_price=base.stop_price)


class DeliveryAnomaly(Strategy):
    """Buy when delivery-pct spikes to unusually high vs its 60-day baseline,
    on a positive close. Premise: institutional accumulation tells.
    Exit: delivery-pct returns to baseline OR price closes below 20-SMA OR stop hit."""

    name = "delivery_anomaly"

    def __init__(self, lookback: int = 60, z_threshold: float = 2.0, atr_mult: float = 2.0):
        self.lookback = lookback
        self.z = z_threshold
        self.atr_mult = atr_mult
        self.indicators = ("atr14", "sma20")

    def signals(self, df: pd.DataFrame) -> StrategySignals:
        dp = df["deliverable_pct"]
        # Need rolling stats on delivery %, computed inline (not a generic indicator)
        roll_mean = dp.rolling(self.lookback, min_periods=self.lookback // 2).mean()
        roll_std = dp.rolling(self.lookback, min_periods=self.lookback // 2).std()
        z_score = (dp - roll_mean) / roll_std
        # Today's z exceeds threshold and we hadn't already triggered yesterday
        spike_today = (z_score >= self.z).fillna(False)
        spike_yesterday = (z_score.shift(1) >= self.z).fillna(False)
        positive_close = (df["close"] > df["close"].shift(1)).fillna(False)
        entry = (spike_today & ~spike_yesterday & positive_close).fillna(False)
        # Exit: delivery normalises (z back below 0.5) OR close drops below 20-SMA
        z_normalised = (z_score < 0.5).fillna(False)
        below_sma20 = (df["close"] < df["sma20"]).fillna(False)
        exit_ = (z_normalised | below_sma20).fillna(False)
        stop = df["close"] - self.atr_mult * df["atr14"]
        return StrategySignals(entry=entry, exit=exit_, stop_price=stop)


STRATEGIES: dict[str, type[Strategy]] = {
    "ema_crossover": EmaCrossover,
    "rsi_mean_reversion": RsiMeanReversion,
    "bollinger_breakout": BollingerBreakout,
    "ema_crossover_filtered": EmaCrossoverFiltered,
    "rsi_mean_reversion_filtered": RsiMeanReversionFiltered,
    "bollinger_breakout_filtered": BollingerBreakoutFiltered,
    "delivery_anomaly": DeliveryAnomaly,
}
