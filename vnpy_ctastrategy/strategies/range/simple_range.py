from __future__ import annotations

from dataclasses import dataclass
from vnpy_ctastrategy import ArrayManager, BarData


@dataclass
class SimpleRangeStrategy:
    """震荡角色：z-score 均值回归 + 严格区间过滤 + 最小持仓周期。"""

    boll_window: int = 26
    boll_dev: float = 2.4
    zscore_entry: float = 2.1
    zscore_exit: float = 0.45
    adx_window: int = 14
    adx_ceiling: float = 17.0
    band_width_ceiling: float = 0.03
    min_hold_bars: int = 3
    reentry_cooldown_bars: int = 2

    virtual_pos: int = 0
    hold_bars: int = 0
    cooldown_bars: int = 0
    mean_value: float = 0.0
    zscore_value: float = 0.0

    def min_bars(self) -> int:
        return max(self.boll_window + 5, self.adx_window + 5)

    def signal(self, am: ArrayManager, bar: BarData) -> float:
        if am.count < self.min_bars():
            return 0.0

        if self.cooldown_bars > 0:
            self.cooldown_bars -= 1

        upper, lower = am.boll(self.boll_window, self.boll_dev)
        self.mean_value = float(am.sma(self.boll_window))
        adx_value = float(am.adx(self.adx_window))

        band_width = max(upper - lower, 1e-12)
        band_width_ratio = band_width / max(bar.close_price, 1e-12)
        sigma = band_width / max(2 * self.boll_dev, 1e-12)
        self.zscore_value = (bar.close_price - self.mean_value) / max(sigma, 1e-12)

        range_filter = (
            adx_value <= self.adx_ceiling
            and band_width_ratio <= self.band_width_ceiling
        )

        if self.virtual_pos == 0:
            self.hold_bars = 0
            if self.cooldown_bars > 0 or not range_filter:
                return 0.0

            if self.zscore_value <= -self.zscore_entry and bar.close_price > lower:
                self.virtual_pos = 1
                self.hold_bars = 0
                return 1.0
            if self.zscore_value >= self.zscore_entry and bar.close_price < upper:
                self.virtual_pos = -1
                self.hold_bars = 0
                return -1.0
            return 0.0

        self.hold_bars += 1

        if self.virtual_pos > 0:
            should_exit = (
                (self.hold_bars >= self.min_hold_bars and self.zscore_value >= -self.zscore_exit)
                or not range_filter
            )
            if should_exit:
                self.virtual_pos = 0
                self.cooldown_bars = self.reentry_cooldown_bars
                return 0.0
            return 1.0

        should_exit = (
            (self.hold_bars >= self.min_hold_bars and self.zscore_value <= self.zscore_exit)
            or not range_filter
        )
        if should_exit:
            self.virtual_pos = 0
            self.cooldown_bars = self.reentry_cooldown_bars
            return 0.0
        return -1.0
