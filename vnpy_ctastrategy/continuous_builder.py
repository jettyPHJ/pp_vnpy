import re
from enum import Enum
from typing import List, Dict, Tuple, Callable, Optional, Any
from datetime import datetime, date
from copy import copy

from vnpy.trader.object import BarData
from vnpy.trader.database import get_database, BaseDatabase


class AdjustMode(Enum):
    """复权模式"""
    ABSOLUTE = "absolute"
    RATIO = "ratio"


class ContinuousBuilder:
    """
    连续合约生成器 (Industrial-Grade Continuous Builder)

    本模块通过严格的日级统计与两阶段复权算法，生成无未来函数的连续合约数据。
    核心解决痛点：
    1. 未来函数：T日使用T-1日收盘后确定的主力合约，T日收盘后决策T+1日合约。
    2. 数据断层：支持主力合约连续缺失容错，防止因单日无数据导致换月误判。
    3. 极端行情：临期强制换月逻辑，确保交割前安全撤离。

    复权模式说明：
    - ABSOLUTE (绝对/加减法): 适合计算盈亏绝对值，但在长期回测中可能出现负价格。
    - RATIO (比率/乘除法): 适合长期趋势回测，能保持K线形态不变，推荐使用。
    """

    def __init__(
        self,
        output: Callable[[str], None],
        adjust_mode: AdjustMode = AdjustMode.RATIO,
        min_vol: int = 1000,
        oi_ratio: float = 1.1,
        vol_ratio: float = 1.2,
        spread_warn: float = 0.05,
        max_missing_current_days: int = 2,
    ) -> None:
        self.output = output
        self.adjust_mode = adjust_mode
        self.min_vol = min_vol
        self.oi_ratio = oi_ratio
        self.vol_ratio = vol_ratio
        self.spread_warn = spread_warn
        self.max_missing_current_days = max_missing_current_days

        # full_symbol -> bars
        self.raw_bars: Dict[str, List[BarData]] = {}
        # full_symbol -> core_symbol
        self.symbol_map: Dict[str, str] = {}

    @staticmethod
    def _to_core_symbol(symbol: str) -> str:
        """统一提取不带交易所后缀的 core symbol"""
        return symbol.split(".")[0].strip()

    def _get_delivery_year_month(self, symbol: str) -> Tuple[int, int]:
        """
        解析交割年月:
        rb2605 -> 2026, 5
        TA605  -> 2026, 5 （兼容旧式三位数字写法，按近十年推断）
        解析失败返回远期默认值
        """
        core_symbol = self._to_core_symbol(symbol)
        match = re.search(r"\d+", core_symbol)
        if not match:
            return 2099, 12

        digits = match.group()

        try:
            if len(digits) == 4:
                year = 2000 + int(digits[:2])
                month = int(digits[2:])
                if 1 <= month <= 12:
                    return year, month

            elif len(digits) == 3:
                # 兼容郑商所部分旧规则：例如 TA605 -> 2026, 5
                year = 2020 + int(digits[0])
                month = int(digits[1:])
                if 1 <= month <= 12:
                    return year, month

        except ValueError:
            pass

        return 2099, 12

    def _is_liquid(self, stats: Dict[str, Any]) -> bool:
        """
        简单流动性过滤：
        - 至少有一定成交量
        - 高低价不完全重合（粗过滤）
        """
        return (stats.get("vol", 0) > self.min_vol and stats.get("high", 0) > 0 and stats.get("low", 0) > 0
                and stats.get("high") != stats.get("low"))

    def _calculate_active_symbols(self, daily_stats: Dict[date, Dict[str, dict]]) -> Dict[date, str]:
        """
        主力合约路由计算引擎

        算法逻辑流程：
        1. 临期优先级最高：若距交割 <= 1个月，新合约OI只要大于旧合约即刻切换（安全第一）。
        2. 常态换月：需同时满足 OI 放大倍数(self.oi_ratio) 且 VOL 放大倍数(self.vol_ratio)。
        3. 容错保活：若当前主力连续缺失数据(<= max_missing_current_days)，强制寻找新主力。

        参数:
            daily_stats: 输入的每日统计数据字典。
                         结构: {date: {full_symbol: {'oi': int, 'vol': int, ...}}}

        返回:
            daily_active_symbol: 输出的每日路由表。
                                 结构: {date: full_symbol}，表示该日应使用的主力合约。

        注意:
            该函数严格遵循“收盘后决策”原则，返回的结果将在下一个交易日生效。
            即：T日的K线数据，使用的是T-1日计算出的合约；T日计算出的新合约，用于T+1日。
        """
        sorted_dates: List[date] = sorted(daily_stats.keys())
        daily_active_symbol: Dict[date, str] = {}

        current_active: Optional[str] = None
        missing_current_days = 0

        for d in sorted_dates:
            stats_dict = daily_stats[d]
            if not stats_dict:
                continue

            if current_active is None:
                current_active = max(stats_dict.keys(), key=lambda s: stats_dict[s]["oi"])
                self.output(f"[{d}] 🏁 初始确立主力合约: {current_active}")

            # 先记录 T 日应使用的主力：这是 T-1 收盘后已决定的结果
            daily_active_symbol[d] = current_active

            cur_stats = stats_dict.get(current_active)
            if cur_stats is None or cur_stats.get("oi", 0) <= 0:
                missing_current_days += 1
                self.output(f"[{d}] ⚠️ 当前主力 {current_active} 当天无有效数据，"
                            f"连续缺失计数={missing_current_days}")

                # 缺失不立刻切，避免单日数据异常造成误换月
                if missing_current_days <= self.max_missing_current_days:
                    continue

                # 连续缺失超过阈值，尝试 fallback
                fallback_candidates = [sym for sym, s in stats_dict.items() if self._is_liquid(s) and s.get("oi", 0) > 0]
                if fallback_candidates:
                    fallback_sym = max(fallback_candidates, key=lambda s: stats_dict[s]["oi"])
                    if fallback_sym != current_active:
                        self.output(f"[{d} 收盘] ⚠️ 当前主力连续缺失超过阈值，"
                                    f"触发容错切换: {current_active} -> {fallback_sym}")
                        current_active = fallback_sym
                        missing_current_days = 0
                continue
            else:
                missing_current_days = 0

            current_oi = cur_stats["oi"]
            current_vol = cur_stats["vol"]

            best_sym = max(stats_dict.keys(), key=lambda s: stats_dict[s]["oi"])
            best_stats = stats_dict[best_sym]

            if best_sym == current_active:
                continue

            if not self._is_liquid(best_stats):
                continue

            del_year, del_month = self._get_delivery_year_month(current_active)
            months_to_delivery = (del_year - d.year) * 12 + (del_month - d.month)
            is_expiring = months_to_delivery <= 1

            spread_ratio = 0.0
            if cur_stats["close"] > 0:
                spread_ratio = abs(best_stats["close"] - cur_stats["close"]) / cur_stats["close"]

            # 临期优先考虑安全撤离
            if is_expiring:
                if best_stats["oi"] > current_oi:
                    self.output(f"[{d} 收盘] 🚨 触发强制移仓 (距交割≤1个月): "
                                f"{current_active} -> {best_sym}")
                    if spread_ratio > self.spread_warn:
                        self.output(f"   ⚠️ 危险滑点警告：远近月价差达 {spread_ratio * 100:.2f}%！")
                    current_active = best_sym
                continue

            # 正常情况下使用量价双确认
            if (best_stats["oi"] > current_oi * self.oi_ratio and best_stats["vol"] > current_vol * self.vol_ratio):
                self.output(f"[{d} 收盘] 🔄 触发量价换月: {current_active} -> {best_sym}")
                current_active = best_sym

        return daily_active_symbol

    def load_and_build(self, physical_symbols: List[str], exchange, interval: str, start: datetime,
                       end: datetime) -> Tuple[List[BarData], Dict[datetime, str], Dict[Tuple[str, datetime], BarData]]:
        """
        构建连续合约的主流程

        流程步骤：
        1. 数据加载: 从数据库读取物理合约数据，并进行清洗（剔除0/负价格）。
        2. 统计聚合: 将分钟/小时Bar聚合成日级统计量(daily_stats)，用于换月判断。
        3. 路由计算: 调用 _calculate_active_symbols 确定每日应使用的合约。
        4. 复权拼接:
           - 遍历所有原始Bar，仅保留路由表中选中的合约Bar。
           - 两阶段复权:
             a. 阶段一(回溯): 遇到换月点，计算因子并累加(cumulative_diff/ratio)，将新合约价格压回旧尺度。
             b. 阶段二(拉升): 遍历结束后，将所有历史价格整体拉升至最新合约的尺度(前复权)。

        参数:
            physical_symbols: 物理合约代码列表 (如 ['IF2106', 'IF2109'])
            exchange: 交易所
            interval: K线周期
            start/end: 时间范围

        返回:
            continuous_bars: 生成的连续合约K线列表 (已复权)。
            routing_schedule: 时间戳 -> 主力合约代码 的映射表 (用于分析换月点)。
            physical_bars_dict: 原始物理K线字典 (用于调试和校验)。
        """
        self.raw_bars.clear()
        self.symbol_map.clear()

        self.output(f"启动构建，复权模式: {self.adjust_mode.value.upper()}")
        database: BaseDatabase = get_database()

        physical_bars_dict: Dict[Tuple[str, datetime], BarData] = {}

        # 1. 加载并清洗原始 bars
        for full_symbol in physical_symbols:
            core_symbol = self._to_core_symbol(full_symbol)
            self.symbol_map[full_symbol] = core_symbol

            bars: List[BarData] = database.load_bar_data(core_symbol, exchange, interval, start, end)

            if not bars:
                self.output(f"⚠️ 未加载到数据: {full_symbol}")
                continue

            clean_bars = [b for b in bars if b.low_price > 0 and b.high_price > 0]
            clean_bars.sort(key=lambda x: x.datetime)

            if not clean_bars:
                self.output(f"⚠️ 清洗后为空: {full_symbol}")
                continue

            self.raw_bars[full_symbol] = clean_bars

            for bar in clean_bars:
                physical_bars_dict[(full_symbol, bar.datetime)] = bar

        if not self.raw_bars:
            self.output("⚠️ 没有可用原始数据，构建结束")
            return [], {}, {}

        # 2. 聚合 daily_stats
        daily_stats: Dict[date, Dict[str, dict]] = {}
        for full_symbol, bars in self.raw_bars.items():
            for bar in bars:
                d = bar.datetime.date()

                if d not in daily_stats:
                    daily_stats[d] = {}

                if full_symbol not in daily_stats[d]:
                    daily_stats[d][full_symbol] = {
                        "oi": 0,
                        "vol": 0,
                        "high": bar.high_price,
                        "low": bar.low_price,
                        "close": bar.close_price,
                    }

                ds = daily_stats[d][full_symbol]
                ds["oi"] = bar.open_interest  # 依赖已排序，最终保留收盘 OI
                ds["vol"] += bar.volume
                ds["high"] = max(ds["high"], bar.high_price)
                ds["low"] = min(ds["low"], bar.low_price)
                ds["close"] = bar.close_price  # 最终保留收盘价

        # 3. 计算每日应使用的主力
        daily_active_symbol: Dict[date, str] = self._calculate_active_symbols(daily_stats)

        # 4. 扁平化所有 bar
        all_bars_flat: List[Tuple[str, BarData]] = []
        for full_symbol, bars in self.raw_bars.items():
            for bar in bars:
                all_bars_flat.append((full_symbol, bar))
        all_bars_flat.sort(key=lambda x: x[1].datetime)

        self.output("正在拼接K线并应用前复权...")

        continuous_bars: List[BarData] = []
        routing_schedule: Dict[datetime, str] = {}

        cumulative_diff: float = 0.0
        cumulative_ratio: float = 1.0

        last_active: Optional[str] = None
        last_close_for_old: float = 0.0

        # 阶段一：以最早段为基准，后续新段不断压回旧尺度
        for full_symbol, bar in all_bars_flat:
            d = bar.datetime.date()
            active_full_symbol = daily_active_symbol.get(d)

            if active_full_symbol is None:
                continue

            # 只有当前日期被路由选中的合约 bar 才能进入连续序列
            if full_symbol != active_full_symbol:
                continue

            routing_schedule[bar.datetime] = active_full_symbol

            # 若当前 bar 所属合约已变，说明这里是切换后第一根被采纳的新主力 bar
            if last_active is not None and active_full_symbol != last_active and last_close_for_old > 0:
                if self.adjust_mode == AdjustMode.ABSOLUTE:
                    spread = bar.open_price - last_close_for_old
                    cumulative_diff += spread
                else:
                    if last_close_for_old <= 0:
                        self.output(f"⚠️ 跳过异常换月比例计算: old_close={last_close_for_old}, "
                                    f"datetime={bar.datetime}")
                    else:
                        ratio = bar.open_price / last_close_for_old
                        cumulative_ratio *= ratio

            adj_bar = copy(bar)
            adj_bar.symbol = "CONTINUOUS"

            if self.adjust_mode == AdjustMode.ABSOLUTE:
                adj_bar.open_price -= cumulative_diff
                adj_bar.high_price -= cumulative_diff
                adj_bar.low_price -= cumulative_diff
                adj_bar.close_price -= cumulative_diff
            else:
                adj_bar.open_price /= cumulative_ratio
                adj_bar.high_price /= cumulative_ratio
                adj_bar.low_price /= cumulative_ratio
                adj_bar.close_price /= cumulative_ratio

            continuous_bars.append(adj_bar)

            last_active = active_full_symbol
            last_close_for_old = bar.close_price

        # 阶段二：整体抬到最新段尺度，形成前复权
        has_alerted_negative = False

        if continuous_bars:
            if self.adjust_mode == AdjustMode.ABSOLUTE and cumulative_diff != 0:
                for c_bar in continuous_bars:
                    c_bar.open_price += cumulative_diff
                    c_bar.high_price += cumulative_diff
                    c_bar.low_price += cumulative_diff
                    c_bar.close_price += cumulative_diff

                    if not has_alerted_negative and c_bar.low_price < 0:
                        self.output(f"⚠️ 绝对复权负价警告: {c_bar.datetime} "
                                    f"最低价跌穿零轴({c_bar.low_price:.2f})！建议切换为等比复权。")
                        has_alerted_negative = True

            elif self.adjust_mode == AdjustMode.RATIO and cumulative_ratio != 1.0:
                for c_bar in continuous_bars:
                    c_bar.open_price *= cumulative_ratio
                    c_bar.high_price *= cumulative_ratio
                    c_bar.low_price *= cumulative_ratio
                    c_bar.close_price *= cumulative_ratio

        return continuous_bars, routing_schedule, physical_bars_dict
