import re
from enum import Enum
from typing import List, Dict, Tuple, Callable, Optional
from datetime import datetime, date
from copy import copy
from vnpy.trader.object import BarData
from vnpy.trader.database import get_database, BaseDatabase


class AdjustMode(Enum):
    """复权模式配置"""
    ABSOLUTE = "absolute"  # 绝对值复权 (加减差价)
    RATIO = "ratio"  # 等比复权 (乘除比例) - 主流机构推荐


class ContinuousBuilder:
    """机构级：连续合约与动态路由生成器"""

    def __init__(self, output: Callable, adjust_mode: AdjustMode = AdjustMode.RATIO):
        self.output = output
        self.adjust_mode = adjust_mode  # 默认使用机构级的等比复权
        self.raw_bars: Dict[str, List[BarData]] = {}

    def _get_delivery_year_month(self, symbol: str) -> Tuple[int, int]:
        """解析合约的交割年份与月份 (例如 'rb2605' -> 2026, 5)"""
        match = re.search(r'\d+', symbol)
        if match:
            digits = match.group()
            if len(digits) == 4:
                return 2000 + int(digits[:2]), int(digits[2:])
            elif len(digits) == 3:  # 兼容郑商所旧规则 (如 TA605 -> 26年5月)
                return 2020 + int(digits[0]), int(digits[1:])
        return 2099, 12  # 解析失败则默认远期

    def _calculate_active_symbols(self, daily_stats: Dict[date, Dict[str, dict]]) -> Dict[date, str]:
        """
        三维机构级换月路由：
        1. 流动性双重验证：持仓量 > 1.1倍 且 成交量 > 1.2倍。
        2. 涨跌停防御：新合约 High == Low 时拒绝换月。
        3. 临期强制逃亡：进入交割前1个月，无视阈值强制切向最高流动性远月。
        """
        sorted_dates: List[date] = sorted(list(daily_stats.keys()))
        daily_active_symbol: Dict[date, str] = {}
        current_active: Optional[str] = None

        for d in sorted_dates:
            stats_dict = daily_stats[d]

            if current_active is None:
                current_active = max(stats_dict.keys(), key=lambda s: stats_dict[s]['oi'])
                daily_active_symbol[d] = current_active
                self.output(f"[{d}] 🏁 初始确立主力合约: {current_active}")
                continue

            # 日内不切，沿用昨天决定的主力
            daily_active_symbol[d] = current_active

            # ==========================================
            # 每日收盘后，评估是否触发换月 (次日生效)
            # ==========================================
            cur_stats = stats_dict.get(current_active, {'oi': 0, 'vol': 0})
            current_oi, current_vol = cur_stats['oi'], cur_stats['vol']

            # 寻找当下 OI 最大的合约作为潜在目标
            best_sym = max(stats_dict.keys(), key=lambda s: stats_dict[s]['oi'])
            best_stats = stats_dict[best_sym]

            if best_sym == current_active:
                continue

            # 1. 涨跌停流动性检验
            is_liquid = (best_stats['high'] != best_stats['low']) and (best_stats['vol'] > 0)

            # 2. 交割期强制逃亡检验 (临近1个月)
            del_year, del_month = self._get_delivery_year_month(current_active)
            is_expiring = (d.year > del_year) or (d.year == del_year and d.month >= del_month - 1)

            # 换月决策树
            if is_expiring and is_liquid:
                # 临近交割，只要远月比现在的废壳大，赶紧跑！
                if best_stats['oi'] > current_oi:
                    self.output(f"[{d} 收盘] 🚨 触发强制移仓 (进入交割月): {current_active} -> {best_sym}")
                    current_active = best_sym

            elif is_liquid:
                # 正常时期：OI超1.1倍 且 成交量超1.2倍 才算真实主力资金移库
                if best_stats['oi'] > current_oi * 1.1 and best_stats['vol'] > current_vol * 1.2:
                    self.output(f"[{d} 收盘] 🔄 触发量价换月: {best_sym} (持仓/成交已全面反超)")
                    current_active = best_sym

        return daily_active_symbol

    def load_and_build(self, physical_symbols: List[str], exchange, interval: str, start: datetime,
                       end: datetime) -> Tuple[List[BarData], Dict[datetime, str], Dict[Tuple[str, datetime], BarData]]:

        self.output(f"启动构建，复权模式: {self.adjust_mode.value.upper()}")
        database: BaseDatabase = get_database()
        physical_bars_dict: Dict[Tuple[str, datetime], BarData] = {}

        # 1. 加载数据并实施极简清洗
        for p_symbol in physical_symbols:
            req_symbol: str = p_symbol.split(".")[0]
            bars: List[BarData] = database.load_bar_data(req_symbol, exchange, interval, start, end)
            if bars:
                # 简单清洗：过滤掉极度异常的错价包 (价格<=0)
                clean_bars = [b for b in bars if b.low_price > 0 and b.high_price > 0]
                self.raw_bars[p_symbol] = clean_bars
                for bar in clean_bars:
                    physical_bars_dict[(p_symbol, bar.datetime)] = bar

        if not self.raw_bars:
            return [], {}, {}

        # 2. 梳理每日多维度特征，送入路由引擎
        daily_stats: Dict[date, Dict[str, dict]] = {}
        for sym, bars in self.raw_bars.items():
            for bar in bars:
                d: date = bar.datetime.date()
                if d not in daily_stats:
                    daily_stats[d] = {}
                # 同一日期的最后一根 Bar 会覆盖，从而得到收盘数据和全天极大/极小值
                if sym not in daily_stats[d]:
                    daily_stats[d][sym] = {
                        'oi': 0,
                        'vol': 0,
                        'high': bar.high_price,
                        'low': bar.low_price,
                        'close': bar.close_price
                    }

                ds = daily_stats[d][sym]
                ds['oi'] = bar.open_interest  # 收盘OI
                ds['vol'] += bar.volume  # 累加日内Volume
                ds['high'] = max(ds['high'], bar.high_price)
                ds['low'] = min(ds['low'], bar.low_price)
                ds['close'] = bar.close_price  # 最终收盘价

        daily_active_symbol = self._calculate_active_symbols(daily_stats)

        # 3. 核心复权拼接逻辑
        self.output("正在拼接K线并应用前复权...")
        continuous_bars: List[BarData] = []
        routing_schedule: Dict[datetime, str] = {}

        all_bars_flat: List[BarData] = []
        for bars in self.raw_bars.values():
            all_bars_flat.extend(bars)
        all_bars_flat.sort(key=lambda x: x.datetime)

        cumulative_diff: float = 0.0  # 绝对值复权累积
        cumulative_ratio: float = 1.0  # 等比复权累积
        last_active: Optional[str] = None
        last_close_for_old: float = 0.0

        # 阶段一：顺时针抹平历史 (后复权锚定起点)
        for bar in all_bars_flat:
            d: date = bar.datetime.date()
            active_sym: str = daily_active_symbol.get(d, "")
            routing_schedule[bar.datetime] = active_sym

            if bar.symbol == active_sym.split('.')[0]:
                if last_active and active_sym != last_active and last_close_for_old > 0:
                    # 发生换月，计算跳空 (新开 - 老收)
                    if self.adjust_mode == AdjustMode.ABSOLUTE:
                        spread = bar.open_price - last_close_for_old
                        cumulative_diff += spread
                    elif self.adjust_mode == AdjustMode.RATIO:
                        ratio = bar.open_price / last_close_for_old
                        cumulative_ratio *= ratio

                adj_bar: BarData = copy(bar)
                adj_bar.symbol = "CONTINUOUS"

                # 应用当前累计调整
                if self.adjust_mode == AdjustMode.ABSOLUTE:
                    adj_bar.open_price -= cumulative_diff
                    adj_bar.high_price -= cumulative_diff
                    adj_bar.low_price -= cumulative_diff
                    adj_bar.close_price -= cumulative_diff
                elif self.adjust_mode == AdjustMode.RATIO:
                    adj_bar.open_price /= cumulative_ratio
                    adj_bar.high_price /= cumulative_ratio
                    adj_bar.low_price /= cumulative_ratio
                    adj_bar.close_price /= cumulative_ratio

                continuous_bars.append(adj_bar)
                last_active = active_sym
                last_close_for_old = bar.close_price

        # 阶段二：O(N) 极速转【前复权】
        # 将所有历史数据反向抬高/放大，使最新K线完美对齐真实物理盘口
        has_alerted_negative = False

        if continuous_bars:
            for c_bar in continuous_bars:
                if self.adjust_mode == AdjustMode.ABSOLUTE and cumulative_diff != 0:
                    c_bar.open_price += cumulative_diff
                    c_bar.high_price += cumulative_diff
                    c_bar.low_price += cumulative_diff
                    c_bar.close_price += cumulative_diff

                    if not has_alerted_negative and c_bar.low_price < 0:
                        self.output(f"⚠️ 绝对复权负价警告: {c_bar.datetime} 最低价跌穿零轴({c_bar.low_price:.2f})！建议切换为等比复权。")
                        has_alerted_negative = True

                elif self.adjust_mode == AdjustMode.RATIO and cumulative_ratio != 1.0:
                    c_bar.open_price *= cumulative_ratio
                    c_bar.high_price *= cumulative_ratio
                    c_bar.low_price *= cumulative_ratio
                    c_bar.close_price *= cumulative_ratio

        return continuous_bars, routing_schedule, physical_bars_dict
