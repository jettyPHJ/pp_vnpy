from __future__ import annotations

from dataclasses import dataclass
from vnpy_ctastrategy import ArrayManager, BarData


@dataclass
class SimpleDefenseStrategy:
    """防守角色：默认空仓；若开启试探，仅给非常轻的顺势信号。"""

    enable_probe: bool = False
    fast_window: int = 10
    slow_window: int = 30
    rsi_window: int = 10
    long_rsi_threshold: float = 60.0
    short_rsi_threshold: float = 40.0
    probe_signal: float = 0.25

    virtual_pos: int = 0

    def min_bars(self) -> int:
        return max(self.fast_window + 5, self.slow_window + 5, self.rsi_window + 5)

    def signal(self, am: ArrayManager, bar: BarData) -> float:
        if not self.enable_probe:
            self.virtual_pos = 0
            return 0.0

        if am.count < self.min_bars():
            return 0.0

        fast_ma = float(am.sma(self.fast_window))
        slow_ma = float(am.sma(self.slow_window))
        rsi_value = float(am.rsi(self.rsi_window))

        if fast_ma > slow_ma and rsi_value >= self.long_rsi_threshold:
            self.virtual_pos = 1
            return self.probe_signal
        if fast_ma < slow_ma and rsi_value <= self.short_rsi_threshold:
            self.virtual_pos = -1
            return -self.probe_signal

        self.virtual_pos = 0
        return 0.0
