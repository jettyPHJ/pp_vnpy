from typing import List, Dict, Tuple, Callable, Optional
from datetime import datetime, date
from copy import copy
from vnpy.trader.object import BarData
from vnpy.trader.database import get_database, BaseDatabase


class ContinuousBuilder:
    """连续合约与路由表生成器 (机构级)"""

    def __init__(self, output: Callable):
        self.output = output
        self.raw_bars: Dict[str, List[BarData]] = {}

    def _calculate_active_symbols(self, daily_close_oi: Dict[date, Dict[str, int]]) -> Dict[date, str]:
        """
        核心换月逻辑 (独立抽离)：
        1. 首次上市：以首日收盘持仓量最大者作为初始主力。
        2. 换月阈值：同品种其他合约收盘持仓量 > 当前主力持仓量 * 1.1。
        3. 换月生效：次日生效（日内绝对不换月）。
        """
        sorted_dates: List[date] = sorted(list(daily_close_oi.keys()))
        daily_active_symbol: Dict[date, str] = {}
        current_active: Optional[str] = None

        for d in sorted_dates:
            oi_dict: Dict[str, int] = daily_close_oi[d]

            if current_active is None:
                # 初始确立：首次上市，选当日最大OI
                current_active = max(oi_dict, key=oi_dict.get)
                daily_active_symbol[d] = current_active
                self.output(f"[{d}] 首次确立初始主力合约: {current_active}")
            else:
                # 今日沿用昨日决定的主力合约（确保日内不切换）
                daily_active_symbol[d] = current_active

            # ==========================================
            # 在今日收盘时，评估是否需要为【下一交易日】切换主力
            # ==========================================
            current_active_oi: int = oi_dict.get(current_active, 0)

            # 找到今日收盘时，持仓量最大的合约
            best_sym: str = max(oi_dict, key=oi_dict.get)
            best_oi: int = oi_dict.get(best_sym, 0)

            # 触发条件：非当前主力，且持仓量超过当前主力的 1.1 倍
            if best_sym != current_active and best_oi > current_active_oi * 1.1:
                self.output(
                    f"[{d} 收盘] 触发换月信号 🔄 : {best_sym} 持仓量({best_oi}) 已超过原主力 {current_active}({current_active_oi}) 的1.1倍。")
                self.output(f"  -> 将于下一交易日正式切换为主力。")
                # 更新 current_active，它将在下一天 (下一轮循环) 生效
                current_active = best_sym

        return daily_active_symbol

    def load_and_build(self, physical_symbols: List[str], exchange, interval: str, start: datetime,
                       end: datetime) -> Tuple[List[BarData], Dict[datetime, str], Dict[Tuple[str, datetime], BarData]]:

        database: BaseDatabase = get_database()
        physical_bars_dict: Dict[Tuple[str, datetime], BarData] = {}

        # 1. 从数据库拉取所有物理合约
        for p_symbol in physical_symbols:
            self.output(f"正在拉取底层物理合约: {p_symbol}")
            req_symbol: str = p_symbol.split(".")[0]

            bars: List[BarData] = database.load_bar_data(req_symbol, exchange, interval, start, end)
            if bars:
                self.raw_bars[p_symbol] = bars
                for bar in bars:
                    physical_bars_dict[(p_symbol, bar.datetime)] = bar

        if not self.raw_bars:
            return [], {}, {}

        # 2. 梳理每日收盘持仓量 (OI)，并调用独立换月逻辑
        self.output("正在扫描每日收盘持仓量，计算主力换月路径...")
        daily_close_oi: Dict[date, Dict[str, int]] = {}

        for sym, bars in self.raw_bars.items():
            for bar in bars:
                d: date = bar.datetime.date()
                if d not in daily_close_oi:
                    daily_close_oi[d] = {}
                # 由于加载的历史K线是按时间正序排列的，
                # 同一日期的最后一个 bar.open_interest 就会自然覆盖前面的，从而得到收盘持仓量
                daily_close_oi[d][sym] = bar.open_interest

        # 调用独立方法生成主力日历
        daily_active_symbol = self._calculate_active_symbols(daily_close_oi)

        # 3. 拼接并复权
        self.output("正在拼接K线，并进行基差【前复权】处理...")
        continuous_bars: List[BarData] = []
        routing_schedule: Dict[datetime, str] = {}

        all_bars_flat: List[BarData] = []
        for bars in self.raw_bars.values():
            all_bars_flat.extend(bars)
        all_bars_flat.sort(key=lambda x: x.datetime)

        cumulative_adjustment: float = 0.0
        last_active: Optional[str] = None
        last_close_for_old: float = 0.0

        # 第一阶段：按时间线计算跳空缺口（计算后复权偏移量）
        for bar in all_bars_flat:
            d: date = bar.datetime.date()
            active_sym: str = daily_active_symbol.get(d, "")
            routing_schedule[bar.datetime] = active_sym

            if bar.symbol == active_sym.split('.')[0]:
                if last_active and active_sym != last_active:
                    # 发生换月，计算跳空缺口 (新合约开盘 - 老合约真实收盘)
                    spread: float = bar.open_price - last_close_for_old
                    cumulative_adjustment += spread

                adj_bar: BarData = copy(bar)
                adj_bar.symbol = "CONTINUOUS"

                # 暂时向下平移消除缺口
                adj_bar.open_price -= cumulative_adjustment
                adj_bar.high_price -= cumulative_adjustment
                adj_bar.low_price -= cumulative_adjustment
                adj_bar.close_price -= cumulative_adjustment

                continuous_bars.append(adj_bar)
                last_active = active_sym
                last_close_for_old = bar.close_price

        # 第二阶段：O(N) 极速转为【前复权】并进行负价预警检查
        if continuous_bars and cumulative_adjustment != 0:
            has_alerted_negative = False

            for c_bar in continuous_bars:
                # 将累积的总偏差整体抬高，使得最新的一根K线完美对齐真实物理盘口
                c_bar.open_price += cumulative_adjustment
                c_bar.high_price += cumulative_adjustment
                c_bar.low_price += cumulative_adjustment
                c_bar.close_price += cumulative_adjustment

                # 【新增】：价格异常检查
                if not has_alerted_negative and c_bar.low_price <= 0:
                    self.output(f"⚠️ 警告: 发现连续合约中出现异常价格！(例如 {c_bar.datetime} 的最低价跌至 {c_bar.low_price:.2f})")
                    self.output(f"⚠️ 原因: 该品种可能处于长期深度升水，进行绝对值【前复权】时，早年历史价格被过度向下平移导致穿透零轴。")
                    self.output(f"⚠️ 建议: 若您的技术指标(如ATR/RSI)报错，请考虑忽略或更换等比复权。")
                    has_alerted_negative = True  # 只打印一次，防止日志刷屏

        return continuous_bars, routing_schedule, physical_bars_dict
