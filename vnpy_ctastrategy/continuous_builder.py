from typing import List, Dict, Tuple
from datetime import datetime
from copy import copy
from vnpy.trader.object import BarData
from vnpy.trader.database import get_database

class ContinuousBuilder:
    """机构级：连续合约与路由表生成器"""
    
    def __init__(self, output: callable):
        self.output = output
        self.raw_bars: Dict[str, List[BarData]] = {}
        
    def load_and_build(self, physical_symbols: list, exchange, interval, start, end) -> Tuple[List[BarData], Dict[datetime, str], Dict[Tuple[str, datetime], BarData]]:
        database = get_database()
        physical_bars_dict = {}  # 用于底层真实撮合的字典
        
        # 1. 从数据库拉取所有物理合约
        for p_symbol in physical_symbols:
            self.output(f"Builder正在拉取底层物理合约: {p_symbol}")
            req_symbol = p_symbol.split(".")[0]  # 提取纯代码 "rb2605"
            bars = database.load_bar_data(req_symbol, exchange, interval, start, end)
            if bars:
                self.raw_bars[p_symbol] = bars
                for bar in bars:
                    physical_bars_dict[(p_symbol, bar.datetime)] = bar
                    
        if not self.raw_bars:
            return [], {}, {}

        # 2. 按天对齐，计算每日最大持仓量(OI)寻找主力
        self.output("正在计算持仓量，识别真实主力合约...")
        daily_oi = {}
        for sym, bars in self.raw_bars.items():
            for bar in bars:
                date = bar.datetime.date()
                if date not in daily_oi:
                    daily_oi[date] = {}
                current_max = daily_oi[date].get(sym, 0)
                daily_oi[date][sym] = max(current_max, bar.open_interest)
                
        sorted_dates = sorted(list(daily_oi.keys()))
        daily_active_symbol = {}
        current_active = None
        
        for date in sorted_dates:
            oi_dict = daily_oi[date]
            best_sym = max(oi_dict, key=oi_dict.get)
            # 防抖：只有首次或新主力确立时切换
            if current_active is None or best_sym != current_active:
                current_active = best_sym
            daily_active_symbol[date] = current_active
            
        # 3. 拼接并后复权 (Back-Adjust)
        self.output("正在进行基差平移与后复权处理...")
        continuous_bars: List[BarData] = []
        routing_schedule: Dict[datetime, str] = {}
        
        all_bars_flat = []
        for bars in self.raw_bars.values():
            all_bars_flat.extend(bars)
        all_bars_flat.sort(key=lambda x: x.datetime)
        
        cumulative_adjustment = 0.0
        last_active = None
        last_close_for_old = 0.0
        
        for bar in all_bars_flat:
            date = bar.datetime.date()
            active_sym = daily_active_symbol.get(date)
            routing_schedule[bar.datetime] = active_sym
            
            if bar.symbol == active_sym.split('.')[0]:
                if last_active and active_sym != last_active:
                    # 发生换月，计算跳空缺口
                    spread = bar.open_price - last_close_for_old
                    cumulative_adjustment += spread 
                    
                adj_bar = copy(bar)
                adj_bar.symbol = "CONTINUOUS"
                # 向下平移消除缺口
                adj_bar.open_price -= cumulative_adjustment
                adj_bar.high_price -= cumulative_adjustment
                adj_bar.low_price -= cumulative_adjustment
                adj_bar.close_price -= cumulative_adjustment
                
                continuous_bars.append(adj_bar)
                last_active = active_sym
                last_close_for_old = bar.close_price
                
        return continuous_bars, routing_schedule, physical_bars_dict