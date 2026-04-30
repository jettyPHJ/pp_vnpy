from __future__ import annotations
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime
from typing import Iterator
import bisect
from vnpy.trader.object import TickData


@dataclass
class TickExecutionContext:
    bid1: float
    ask1: float
    bid_vol_1: float
    ask_vol_1: float
    last_price: float
    delta_volume: float
    spread: float
    mid_price: float
    dt: datetime  # 用 dt 避免与 Python 内置 datetime 模块名冲突


class TickReplayStore:
    """
    核心约束：
    1. 每个 Tick 只能被 replay 一次（cursor 单向推进）
    2. replay_window 接受 (after_dt, until_dt]，两端都不为 None（由引擎层保证）
    3. seek_to 用于换月时定位新合约起始游标，不是简单归零
    """

    def __init__(self):
        self._store: dict[str, list[TickData]] = defaultdict(list)
        self._cursor: dict[str, int] = defaultdict(int)
        self._last_volume: dict[str, float] = defaultdict(float)

    def load(self, symbol: str, ticks: list[TickData]) -> None:
        self._store[symbol] = sorted(ticks, key=lambda t: t.datetime)
        self._cursor[symbol] = 0
        self._last_volume[symbol] = 0.0

    def seek_to(self, symbol: str, from_dt: datetime) -> None:
        """
        换月时调用。用 bisect 找到第一个 > from_dt 的 Tick，
        设置 cursor，避免从头扫描历史数据。
        """
        ticks = self._store.get(symbol, [])
        dts = [t.datetime for t in ticks]
        # bisect_right 找第一个 > from_dt 的位置
        idx = bisect.bisect_right(dts, from_dt)
        self._cursor[symbol] = idx
        self._last_volume[symbol] = 0.0  # 新合约量从头计，delta 第一笔不溯历史

    def replay_window(
        self,
        symbol: str,
        after_dt: datetime,
        until_dt: datetime,
    ) -> Iterator[TickExecutionContext]:
        """
        消费 (after_dt, until_dt] 窗口 Tick，cursor 单向推进。
        调用方保证 after_dt < until_dt，且均不为 None。
        """
        ticks = self._store.get(symbol, [])
        idx = self._cursor.get(symbol, 0)

        while idx < len(ticks):
            tick = ticks[idx]
            if tick.datetime <= after_dt:
                idx += 1  # 防御：正常情况不出现，cursor 应已过此点
                continue
            if tick.datetime > until_dt:
                break

            bid1 = tick.bid_price_1 or 0.0
            ask1 = tick.ask_price_1 or 0.0
            lp = tick.last_price or 0.0

            if bid1 > 0 and ask1 > 0:
                spread = ask1 - bid1
                mid_price = (bid1 + ask1) / 2.0
            else:
                spread = 0.0
                mid_price = lp  # 盘口缺失时降级为 last_price

            prev_vol = self._last_volume[symbol]
            delta = max((tick.volume or 0.0) - prev_vol, 0.0)
            self._last_volume[symbol] = tick.volume or 0.0

            yield TickExecutionContext(
                bid1=bid1,
                ask1=ask1,
                bid_vol_1=tick.bid_volume_1 or 0.0,
                ask_vol_1=tick.ask_volume_1 or 0.0,
                last_price=lp,
                delta_volume=delta,
                spread=spread,
                mid_price=mid_price,
                dt=tick.datetime,
            )
            idx += 1

        self._cursor[symbol] = idx
