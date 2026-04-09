from __future__ import annotations

from dataclasses import dataclass
import numpy as np
from vnpy_ctastrategy import ArrayManager, BarData


@dataclass
class SimpleTrendStrategy:
    """趋势角色：前一窗口 Donchian 突破 + 趋势过滤 + ATR 追踪止损。"""

    entry_window: int = 40
    exit_window: int = 14
    atr_window: int = 20
    fast_window: int = 20
    slow_window: int = 60
    adx_window: int = 14
    adx_trend_threshold: float = 25.0
    slope_window: int = 6
    stop_atr_multiple: float = 3.0

    virtual_pos: int = 0
    intra_trade_high: float = 0.0
    intra_trade_low: float = 0.0

    def min_bars(self) -> int:
        return max(
            self.entry_window + 5,
            self.exit_window + 5,
            self.atr_window + 5,
            self.slow_window + self.slope_window + 5,
            self.adx_window + 5,
        )

    def _prev_donchian(self, am: ArrayManager, window: int) -> tuple[float, float]:
        highs = np.array(am.high[-window - 1:-1], dtype=float)
        lows = np.array(am.low[-window - 1:-1], dtype=float)
        return float(np.max(highs)), float(np.min(lows))

    def signal(self, am: ArrayManager, bar: BarData) -> float:
        if am.count < self.min_bars():
            return 0.0

        entry_up, entry_down = self._prev_donchian(am, self.entry_window)
        exit_up, exit_down = self._prev_donchian(am, self.exit_window)

        atr_value = float(am.atr(self.atr_window))
        fast_ma = float(am.sma(self.fast_window))
        slow_ma = float(am.sma(self.slow_window))
        adx_value = float(am.adx(self.adx_window))

        slow_ma_array = am.sma(self.slow_window, array=True)
        slope_ok_long = slope_ok_short = False
        if isinstance(slow_ma_array, np.ndarray) and len(slow_ma_array) >= self.slope_window + 1:
            ma_slope = float(slow_ma_array[-1] - slow_ma_array[-1 - self.slope_window])
            slope_ok_long = ma_slope > 0
            slope_ok_short = ma_slope < 0

        trend_filter_long = (
            fast_ma > slow_ma
            and adx_value >= self.adx_trend_threshold
            and slope_ok_long
        )
        trend_filter_short = (
            fast_ma < slow_ma
            and adx_value >= self.adx_trend_threshold
            and slope_ok_short
        )

        if self.virtual_pos == 0:
            self.intra_trade_high = bar.high_price
            self.intra_trade_low = bar.low_price

            if trend_filter_long and bar.close_price >= entry_up:
                self.virtual_pos = 1
                self.intra_trade_high = bar.high_price
                return 1.0
            if trend_filter_short and bar.close_price <= entry_down:
                self.virtual_pos = -1
                self.intra_trade_low = bar.low_price
                return -1.0
            return 0.0

        if self.virtual_pos > 0:
            self.intra_trade_high = max(self.intra_trade_high, bar.high_price)
            trailing_stop = self.intra_trade_high - atr_value * self.stop_atr_multiple
            hard_exit = max(trailing_stop, exit_down)
            if (not trend_filter_long) or bar.close_price <= hard_exit:
                self.virtual_pos = 0
                return 0.0
            return 1.0

        self.intra_trade_low = min(self.intra_trade_low, bar.low_price)
        trailing_stop = self.intra_trade_low + atr_value * self.stop_atr_multiple
        hard_exit = min(trailing_stop, exit_up)
        if (not trend_filter_short) or bar.close_price >= hard_exit:
            self.virtual_pos = 0
            return 0.0
        return -1.0
