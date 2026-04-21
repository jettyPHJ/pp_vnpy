from collections import defaultdict
from copy import copy
from datetime import (date as Date, datetime, timedelta)
from typing import cast, Any
from collections.abc import Callable
from functools import lru_cache, partial
import traceback

import numpy as np
from pandas import DataFrame, Series
from pandas.core.window import ExponentialMovingWindow
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import empyrical as ep

from vnpy.trader.constant import (Direction, Offset, Exchange, Interval, Status)
from vnpy.trader.database import get_database, BaseDatabase
from vnpy.trader.object import OrderData, TradeData, BarData, TickData
from vnpy.trader.utility import round_to, extract_vt_symbol
from vnpy.trader.optimize import (OptimizationSetting, check_optimization_setting, run_bf_optimization, run_ga_optimization)

from .base import (BacktestingMode, EngineType, STOPORDER_PREFIX, StopOrder, StopOrderStatus, INTERVAL_DELTA_MAP)
from .template import CtaTemplate
from .locale import _
from .continuous_builder import ContinuousBuilder

# 🟢 导入可热插拔的模块接口与 V1 默认实现
from .backtesting_modules import (BaseMarginModel, BaseSlippageModel, BaseExecutionModel, BaseCommissionModel,
                                  V1DefaultMarginModel, V1DefaultSlippageModel, V1DefaultExecutionModel,
                                  V1DefaultCommissionModel)


class BacktestingEngine:
    """"""

    engine_type: EngineType = EngineType.BACKTESTING
    gateway_name: str = "BACKTESTING"

    def __init__(self) -> None:
        """"""
        self.vt_symbol: str = ""
        self.symbol: str = ""
        self.exchange: Exchange
        self.start: datetime
        self.end: datetime
        self.rate: float = 0
        self.slippage: float = 0
        self.size: float = 1
        self.pricetick: float = 0
        self.capital: int = 1_000_000
        self.risk_free: float = 0
        self.annual_days: int = 240
        self.half_life: int = 120
        self.mode: BacktestingMode = BacktestingMode.BAR
        self.by_volume: bool = False  # 🟢 暴露按手收费参数

        self.strategy_class: type[CtaTemplate]
        self.strategy: CtaTemplate
        self.tick: TickData
        self.bar: BarData
        self.datetime: datetime = datetime(1970, 1, 1)

        self.interval: Interval
        self.days: int = 0
        self.callback: Callable
        self.history_data: list = []

        self.stop_order_count: int = 0
        self.stop_orders: dict[str, StopOrder] = {}
        self.active_stop_orders: dict[str, StopOrder] = {}

        self.limit_order_count: int = 0
        self.limit_orders: dict[str, OrderData] = {}
        self.active_limit_orders: dict[str, OrderData] = {}

        self.trade_count: int = 0
        self.trades: dict[str, TradeData] = {}

        self.logs: list = []

        self.daily_results: dict[Date, 'DailyResult'] = {}
        self.daily_df: DataFrame = DataFrame()

        self.physical_symbols = []
        self.physical_bars = {}
        self.physical_positions = {}
        self.routing_schedule = {}
        self.current_physical_symbol = ""

        self._symbol_cache = {}

        self.margin_model: BaseMarginModel = None
        self.slippage_model: BaseSlippageModel = None
        self.execution_model: BaseExecutionModel = None
        self.commission_model: BaseCommissionModel = None

        self.stop_order_history: dict[str, StopOrder] = {}
        self.rollover_logs: list[dict] = []
        self.rollover_skip_logs: list[dict] = []

        # V1.2 审计账本
        self.warmup_days: int = 120
        self.warmup_data: list = []
        self.warmup_blocked_orders: list = []
        self.warmup_interval_mismatch_logs: list = []
        self.bar_route_map: dict = {}
        self.order_audit_logs: dict[str, dict] = {}
        self.limit_order_history: dict[str, OrderData] = {}
        self.data_load_failed: bool = False
        self.data_load_error: str = ""

    def clear_data(self) -> None:
        """Clear all data of last backtesting."""
        self.stop_order_count = 0
        self.stop_orders.clear()
        self.active_stop_orders.clear()
        self.limit_order_count = 0
        self.limit_orders.clear()
        self.active_limit_orders.clear()
        self.trade_count = 0
        self.trades.clear()
        self.logs.clear()
        self.daily_results.clear()

        # 🟢 清空 DataFrame 缓存，不再重复导入
        self.daily_df = DataFrame()

        self.physical_positions.clear()
        self.current_physical_symbol = ""
        self.routing_schedule.clear()
        self.physical_bars.clear()
        self._symbol_cache.clear()

        self.stop_order_history.clear()
        self.rollover_logs.clear()
        self.rollover_skip_logs.clear()
        self.warmup_data.clear()
        self.warmup_blocked_orders.clear()
        self.warmup_interval_mismatch_logs.clear()
        self.bar_route_map.clear()
        self.physical_bars.clear()
        self.order_audit_logs.clear()
        self.limit_order_history.clear()
        self.data_load_failed = False
        self.data_load_error = ""

    def _normalize_datetime(self, dt) -> datetime:
        """
        剥离 tzinfo 标签，兼容 pd.Timestamp。
        前提假设：所有接入的行情 datetime 已是交易所本地时间。
        """
        if hasattr(dt, 'to_pydatetime'):
            dt = dt.to_pydatetime()
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _normalize_vt_symbol(self, symbol: str) -> str:
        if not symbol:
            return ""
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]

        if "." not in symbol:
            result = f"{symbol}.{self.exchange.value}"
        else:
            parts = symbol.split(".")
            result = f"{parts[0]}.{parts[1].upper()}"

        self._symbol_cache[symbol] = result
        return result

    def _get_trading_date(self, dt: datetime) -> Date:
        if dt.hour >= 20:
            if dt.weekday() == 4:
                return (dt + timedelta(days=3)).date()
            elif dt.weekday() == 5:
                return (dt + timedelta(days=2)).date()
            else:
                return (dt + timedelta(days=1)).date()
        elif dt.hour < 8:
            if dt.weekday() == 5:
                return (dt + timedelta(days=2)).date()
            elif dt.weekday() == 6:
                return (dt + timedelta(days=1)).date()
            else:
                return dt.date()
        return dt.date()

    def _get_current_price_offset(self) -> float:
        """获取当前 连续复权价格 与 物理合约价格 的差值"""
        if self.mode != BacktestingMode.BAR:
            return 0.0
        if not self.current_physical_symbol or not hasattr(self, 'bar') or self.bar is None:
            return 0.0

        lookup_dt = self.datetime.replace(microsecond=0)
        phys_bar = self.physical_bars.get((self.current_physical_symbol, lookup_dt))

        # [核心] 严格校验：缺物理K线直接抛出异常阻断，绝不容忍静默污染
        if not phys_bar:
            error_msg = f"缺失物理K线，无法进行价格坐标转换: {self.current_physical_symbol} @ {lookup_dt}"
            self.output(f"[{self.datetime}] ❌ 严重错误: {error_msg}")
            raise RuntimeError(error_msg)

        return self.bar.close_price - phys_bar.close_price

    def _record_limit_order_history(self, order: OrderData) -> None:
        """安全保存限价单快照"""
        snapshot = copy(order)
        snapshot.accounting_price = getattr(order, "accounting_price", order.price)
        snapshot.physical_price = getattr(order, "physical_price", order.price)
        snapshot.price_offset = getattr(order, "price_offset", 0.0)
        self.limit_order_history[order.vt_orderid] = snapshot

    def _record_stop_order_history(self, stop_order: StopOrder) -> None:
        """安全保存止损单快照"""
        snapshot = copy(stop_order)
        snapshot.accounting_price = getattr(stop_order, "accounting_price", stop_order.price)
        snapshot.physical_price = getattr(stop_order, "physical_price", stop_order.price)
        snapshot.price_offset = getattr(stop_order, "price_offset", 0.0)
        self.stop_order_history[stop_order.stop_orderid] = snapshot

    # ====== 策略回调隔离层 ======
    def _call_strategy_on_order(self, order: OrderData) -> None:
        order_for_strategy = copy(order)
        order_for_strategy.price = getattr(order, "accounting_price", order.price)
        self.strategy.on_order(order_for_strategy)

    def _call_strategy_on_stop_order(self, stop_order: StopOrder) -> None:
        so_for_strategy = copy(stop_order)
        so_for_strategy.price = getattr(stop_order, "accounting_price", stop_order.price)
        self.strategy.on_stop_order(so_for_strategy)

    def _call_strategy_on_trade(self, trade: TradeData) -> None:
        trade_for_strategy = copy(trade)
        trade_for_strategy.price = getattr(trade, "accounting_price", trade.price)
        self.strategy.on_trade(trade_for_strategy)

    def set_parameters(self,
                       vt_symbol: str,
                       interval: Interval,
                       start: datetime,
                       rate: float,
                       slippage: float,
                       size: float,
                       pricetick: float,
                       capital: int = 0,
                       end: datetime | None = None,
                       mode: BacktestingMode = BacktestingMode.BAR,
                       risk_free: float = 0,
                       annual_days: int = 240,
                       half_life: int = 120,
                       physical_symbols: list = None,
                       by_volume: bool = False,
                       warmup_days: int = 120) -> None:
        """"""
        self.mode = mode
        self.vt_symbol = vt_symbol
        self.interval = Interval(interval)
        self.rate = rate
        self.slippage = slippage
        self.size = size
        self.pricetick = pricetick
        self.start = start
        self.by_volume = by_volume

        self.symbol, exchange_str = self.vt_symbol.split(".")
        self.exchange = Exchange(exchange_str)

        self.capital = capital

        if not end:
            end = datetime.now()
        self.end = end.replace(hour=23, minute=59, second=59)

        self.mode = mode
        self.risk_free = risk_free
        self.annual_days = annual_days
        self.half_life = half_life

        self.physical_symbols = [self._normalize_vt_symbol(s) for s in physical_symbols] if physical_symbols else []
        self.warmup_days = warmup_days

        # 🟢 初始化加载 V1 默认执行模型，传入按手配置
        self.margin_model = V1DefaultMarginModel()
        self.slippage_model = V1DefaultSlippageModel(self.slippage)
        self.execution_model = V1DefaultExecutionModel()
        self.commission_model = V1DefaultCommissionModel(self.rate, self.by_volume)

    def add_strategy(self, strategy_class: type[CtaTemplate], setting: dict) -> None:
        self.strategy_class = strategy_class
        self.strategy = strategy_class(self, strategy_class.__name__, self.vt_symbol, setting)

    def load_data(self) -> None:
        if not self.physical_symbols:
            self.output(_("未输入物理合约，执行原生单合约加载模式..."))
            self.history_data.clear()
            if self.mode == BacktestingMode.BAR:
                self.history_data = load_bar_data(self.symbol, self.exchange, self.interval, self.start, self.end)
            else:
                self.history_data = load_tick_data(self.symbol, self.exchange, self.start, self.end)
            return

        if self.mode == BacktestingMode.TICK:
            raise NotImplementedError(
                _("❌ 当前 ContinuousBuilder 尚未适配 TICK 级别的物理合约拼接。请使用 BAR 模式，或去除 physical_symbols 执行纯 TICK 单合约回测。"))

        self.output(_("启动自动换月构建流..."))
        builder = ContinuousBuilder(self.output)

        norm_start = self._normalize_datetime(self.start)
        warmup_start = norm_start - timedelta(days=self.warmup_days)

        raw_history, raw_routing, raw_bars = builder.load_and_build(self.physical_symbols, self.exchange, self.interval,
                                                                    warmup_start, self.end)

        # 时区安全切分：暖机段 vs 回测段
        self.warmup_data = [b for b in raw_history if self._normalize_datetime(b.datetime) < norm_start]
        self.history_data = [b for b in raw_history if self._normalize_datetime(b.datetime) >= norm_start]

        # 键值双重标准化：解决大小写与时区导致查不到的 Bug
        self.bar_route_map = {self._normalize_datetime(dt): self._normalize_vt_symbol(sym) for dt, sym in raw_routing.items()}
        self.physical_bars = {
            (self._normalize_vt_symbol(sym), self._normalize_datetime(dt)): bar
            for (sym, dt), bar in raw_bars.items()
        }
        # 同步 routing_schedule 保持向后兼容（_do_rollover 使用它）
        self.routing_schedule = self.bar_route_map

        # 单次遍历校验缺失
        missing_routes = 0
        missing_phys = 0
        for b in self.history_data:
            clean_dt = self._normalize_datetime(b.datetime)
            sym = self.bar_route_map.get(clean_dt)
            if not sym:
                missing_routes += 1
            elif (sym, clean_dt) not in self.physical_bars:
                missing_phys += 1

        if missing_routes > 0 or missing_phys > 0:
            self.data_load_failed = True
            self.data_load_error = f"缺失路由映射 {missing_routes} 根，缺失物理K线 {missing_phys} 根"
            self.output(f"❌ 严重错误：{self.data_load_error}。本次回测已阻断！")
            self.history_data.clear()
            self.warmup_data.clear()
            return

        self.output(_("历史数据构建完毕，回测段：{}根，暖机段：{}根").format(len(self.history_data), len(self.warmup_data)))

    def run_backtesting(self) -> None:
        if self.mode == BacktestingMode.BAR:
            func: Callable[[Any], None] = self.new_bar
        else:
            func = self.new_tick

        self.strategy.on_init()
        self.strategy.inited = True
        self.output(_("策略初始化完成"))

        self.strategy.on_start()
        self.strategy.trading = True
        self.output(_("开始回放历史数据"))

        total_size: int = len(self.history_data)
        if total_size == 0:
            self.output(_("历史数据为空，回测中止"))
            return

        batch_size: int = max(int(total_size / 10), 1)

        for ix, i in enumerate(range(0, total_size, batch_size)):
            batch_data: list = self.history_data[i:i + batch_size]
            for data in batch_data:
                try:
                    func(data)
                except Exception:
                    self.output(_("触发异常，回测终止"))
                    self.output(traceback.format_exc())
                    return

            progress = min((i + batch_size) / total_size, 1)
            progress_bar: str = "=" * int(progress * 10)
            self.output(_("回放进度：{} [{:.0%}]").format(progress_bar, progress))

        self.strategy.on_stop()
        self.output(_("历史数据回放结束"))

    def calculate_result(self) -> DataFrame:
        self.output(_("开始计算逐日盯市盈亏"))

        if not self.trades:
            self.output(_("回测成交记录为空"))

        for trade in self.trades.values():
            if not trade.datetime:
                continue

            d: Date = self._get_trading_date(trade.datetime)
            daily_result: DailyResult = self.daily_results[d]
            daily_result.add_trade(trade)

        pre_close: float = 0
        start_pos: float = 0

        for d in sorted(self.daily_results.keys()):
            daily_result = self.daily_results[d]
            daily_result.calculate_pnl(pre_close, start_pos, self.size, self.commission_model, self.slippage_model)
            pre_close = daily_result.close_price
            start_pos = daily_result.end_pos

        results: defaultdict = defaultdict(list)

        for d in sorted(self.daily_results.keys()):
            daily_result = self.daily_results[d]
            for key, value in daily_result.__dict__.items():
                if key != "trades":
                    results[key].append(value)

        if results:
            self.daily_df = DataFrame.from_dict(results).set_index("date")

        self.output(_("逐日盯市盈亏计算完成"))
        return self.daily_df

    def calculate_statistics(self, df: DataFrame | None = None, output: bool = True) -> dict:
        self.output(_("开始计算策略统计指标"))

        if df is None:
            if self.daily_df.empty:
                self.output(_("回测结果为空，无法计算绩效统计指标"))
                return {}
            df = self.daily_df

        start_date: str = ""
        end_date: str = ""
        total_days: int = 0
        profit_days: int = 0
        loss_days: int = 0
        end_balance: float = 0
        max_drawdown: float = 0
        max_ddpercent: float = 0
        max_drawdown_duration: int = 0
        total_net_pnl: float = 0
        daily_net_pnl: float = 0
        total_commission: float = 0
        daily_commission: float = 0
        total_slippage: float = 0
        daily_slippage: float = 0
        total_turnover: float = 0
        daily_turnover: float = 0
        total_trade_count: int = 0
        daily_trade_count: float = 0
        total_return: float = 0
        annual_return: float = 0
        daily_return: float = 0
        return_std: float = 0
        sharpe_ratio: float = 0
        ewm_sharpe: float = 0
        return_drawdown_ratio: float = 0
        rgr_ratio: float = 0

        positive_balance: bool = False

        if df is not None:
            df["balance"] = df["net_pnl"].cumsum() + self.capital

            pre_balance: Series = df["balance"].shift(1)
            pre_balance.iloc[0] = self.capital
            x: Series = df["balance"] / pre_balance
            x[x <= 0] = np.nan
            df["return"] = np.log(x).fillna(0)

            df["highlevel"] = df["balance"].rolling(min_periods=1, window=len(df), center=False).max()
            df["drawdown"] = df["balance"] - df["highlevel"]
            df["ddpercent"] = df["drawdown"] / df["highlevel"] * 100

            positive_balance = bool((df["balance"] > 0).all())
            if not positive_balance:
                self.output(_("回测中出现爆仓（资金小于等于0），无法计算策略统计指标"))

        if positive_balance:
            start_date = df.index[0]
            end_date = df.index[-1]
            total_days = len(df)
            profit_days = len(df[df["net_pnl"] > 0])
            loss_days = len(df[df["net_pnl"] < 0])

            end_balance = df["balance"].iloc[-1]
            max_drawdown = df["drawdown"].min()
            max_ddpercent = df["ddpercent"].min()
            max_drawdown_end = df["drawdown"].idxmin()

            if isinstance(max_drawdown_end, Date):
                max_drawdown_start = df["balance"][:max_drawdown_end].idxmax()
                max_drawdown_duration = (max_drawdown_end - max_drawdown_start).days
            else:
                max_drawdown_duration = 0

            total_net_pnl = df["net_pnl"].sum()
            daily_net_pnl = total_net_pnl / total_days
            total_commission = df["commission"].sum()
            daily_commission = total_commission / total_days
            total_slippage = df["slippage"].sum()
            daily_slippage = total_slippage / total_days
            total_turnover = df["turnover"].sum()
            daily_turnover = total_turnover / total_days
            total_trade_count = df["trade_count"].sum()
            daily_trade_count = total_trade_count / total_days

            total_return = (end_balance / self.capital - 1) * 100
            annual_return = total_return / total_days * self.annual_days
            daily_return = df["return"].mean() * 100
            return_std = df["return"].std() * 100

            if return_std:
                daily_risk_free: float = self.risk_free / np.sqrt(self.annual_days)
                sharpe_ratio = (daily_return - daily_risk_free) / return_std * np.sqrt(self.annual_days)
                ewm_window: ExponentialMovingWindow = df["return"].ewm(halflife=self.half_life)
                ewm_mean: Series = ewm_window.mean() * 100
                ewm_std: Series = ewm_window.std() * 100
                ewm_sharpe = ((ewm_mean - daily_risk_free) / ewm_std).iloc[-1] * np.sqrt(self.annual_days)
            else:
                sharpe_ratio = 0
                ewm_sharpe = 0

            if max_ddpercent:
                return_drawdown_ratio = -total_return / max_ddpercent
            else:
                return_drawdown_ratio = 0

            cagr_value: float = annual_return / 100
            if return_std > 0:
                stability_return: float = 1 / (1 + return_std / 100)
            else:
                stability_return = 0

            returns_series: Series = df["return"]
            annual_downside_risk: float = float(ep.downside_risk(returns_series, required_return=0, period='daily'))
            return_skew: float = cast(float, returns_series.skew())
            return_kurt: float = cast(float, returns_series.kurt())
            cvar_95 = float(ep.conditional_value_at_risk(returns_series, cutoff=0.05))

            rgr_ratio = calc_rgr_ratio(cagr_value, stability_return, annual_downside_risk, max_ddpercent, return_skew,
                                       return_kurt, cvar_95)

        if output:
            self.output("-" * 30)
            self.output(_("首个交易日：\t{}").format(start_date))
            self.output(_("最后交易日：\t{}").format(end_date))
            self.output(_("总交易日：\t{}").format(total_days))
            self.output(_("盈利交易日：\t{}").format(profit_days))
            self.output(_("亏损交易日：\t{}").format(loss_days))
            self.output(_("起始资金：\t{:,.2f}").format(self.capital))
            self.output(_("结束资金：\t{:,.2f}").format(end_balance))
            self.output(_("总收益率：\t{:,.2f}%").format(total_return))
            self.output(_("年化收益：\t{:,.2f}%").format(annual_return))
            self.output(_("最大回撤: \t{:,.2f}").format(max_drawdown))
            self.output(_("百分比最大回撤: {:,.2f}%").format(max_ddpercent))
            self.output(_("最大回撤天数: \t{}").format(max_drawdown_duration))
            self.output(_("总盈亏：\t{:,.2f}").format(total_net_pnl))
            self.output(_("总手续费：\t{:,.2f}").format(total_commission))
            self.output(_("总滑点：\t{:,.2f}").format(total_slippage))
            self.output(_("总成交金额：\t{:,.2f}").format(total_turnover))
            self.output(_("总成交笔数：\t{}").format(total_trade_count))
            self.output(_("日均盈亏：\t{:,.2f}").format(daily_net_pnl))
            self.output(_("日均手续费：\t{:,.2f}").format(daily_commission))
            self.output(_("日均滑点：\t{:,.2f}").format(daily_slippage))
            self.output(_("日均成交金额：\t{:,.2f}").format(daily_turnover))
            self.output(_("日均成交笔数：\t{}").format(daily_trade_count))
            self.output(_("日均收益率：\t{:,.2f}%").format(daily_return))
            self.output(_("收益标准差：\t{:,.2f}%").format(return_std))
            self.output(f"Sharpe Ratio：\t{sharpe_ratio:,.2f}")
            self.output(f"EWM Sharpe：\t{ewm_sharpe:,.2f}")
            self.output(_("收益回撤比：\t{:,.2f}").format(return_drawdown_ratio))
            self.output(f"RGR Ratio：\t{rgr_ratio:,.2f}")

        statistics: dict = {
            "start_date": start_date,
            "end_date": end_date,
            "total_days": total_days,
            "profit_days": profit_days,
            "loss_days": loss_days,
            "capital": self.capital,
            "end_balance": end_balance,
            "max_drawdown": max_drawdown,
            "max_ddpercent": max_ddpercent,
            "max_drawdown_duration": max_drawdown_duration,
            "total_net_pnl": total_net_pnl,
            "daily_net_pnl": daily_net_pnl,
            "total_commission": total_commission,
            "daily_commission": daily_commission,
            "total_slippage": total_slippage,
            "daily_slippage": daily_slippage,
            "total_turnover": total_turnover,
            "daily_turnover": daily_turnover,
            "total_trade_count": total_trade_count,
            "daily_trade_count": daily_trade_count,
            "total_return": total_return,
            "annual_return": annual_return,
            "daily_return": daily_return,
            "return_std": return_std,
            "sharpe_ratio": sharpe_ratio,
            "ewm_sharpe": ewm_sharpe,
            "return_drawdown_ratio": return_drawdown_ratio,
            "rgr_ratio": rgr_ratio,
        }

        for key, value in statistics.items():
            if value in (np.inf, -np.inf):
                value = 0
            statistics[key] = np.nan_to_num(value)

        self.output(_("策略统计指标计算完成"))
        return statistics

    def show_chart(self, df: DataFrame | None = None) -> go.Figure:
        if df is None: df = self.daily_df
        if df.empty: return

        # 使用净值代替余额
        plot_df = df.copy()
        if "balance" not in plot_df.columns:
            plot_df["balance"] = plot_df["net_pnl"].cumsum() + self.capital

        plot_df["net_value"] = plot_df["balance"] / self.capital
        plot_df["highlevel"] = plot_df["balance"].cummax()
        plot_df["ddpercent"] = (plot_df["balance"] - plot_df["highlevel"]) / plot_df["highlevel"] * 100

        fig = make_subplots(rows=4,
                            cols=1,
                            subplot_titles=["Net Value", "Drawdown (%)", "Daily Pnl", "Cumulative Pnl"],
                            vertical_spacing=0.06)

        fig.add_trace(go.Scatter(x=plot_df.index, y=plot_df["net_value"], mode="lines", name="Net Value"), row=1, col=1)
        fig.add_trace(go.Scatter(x=plot_df.index,
                                 y=plot_df["ddpercent"],
                                 fillcolor="rgba(255,0,0,0.3)",
                                 fill='tozeroy',
                                 mode="lines",
                                 name="Drawdown(%)"),
                      row=2,
                      col=1)
        fig.add_trace(go.Bar(y=plot_df["net_pnl"], name="Daily Pnl"), row=3, col=1)
        fig.add_trace(go.Scatter(x=plot_df.index,
                                 y=plot_df["net_pnl"].cumsum(),
                                 fill='tozeroy',
                                 mode="lines",
                                 name="Cumulative Pnl"),
                      row=4,
                      col=1)

        fig.update_layout(height=1000, width=1000)
        return fig

    def run_bf_optimization(self,
                            optimization_setting: OptimizationSetting,
                            output: bool = True,
                            max_workers: int | None = None) -> list:
        if not check_optimization_setting(optimization_setting):
            return []
        evaluate_func: Callable = wrap_evaluate(self, optimization_setting.target_name)
        results: list = run_bf_optimization(evaluate_func,
                                            optimization_setting,
                                            get_target_value,
                                            max_workers=max_workers,
                                            output=self.output)
        if output:
            for result in results:
                self.output(_("参数：{}, 目标：{}").format(result[0], result[1]))
        return results

    run_optimization = run_bf_optimization

    def run_ga_optimization(self,
                            optimization_setting: OptimizationSetting,
                            output: bool = True,
                            max_workers: int | None = None,
                            pop_size: int = 100,
                            ngen: int = 30,
                            mu: int | None = None,
                            lambda_: int | None = None,
                            cxpb: float = 0.95,
                            mutpb: float | None = None,
                            indpb: float = 1.0) -> list:
        if not check_optimization_setting(optimization_setting):
            return []
        evaluate_func: Callable = wrap_evaluate(self, optimization_setting.target_name)
        results: list = run_ga_optimization(evaluate_func,
                                            optimization_setting,
                                            get_target_value,
                                            max_workers=max_workers,
                                            pop_size=pop_size,
                                            ngen=ngen,
                                            mu=mu,
                                            lambda_=lambda_,
                                            cxpb=cxpb,
                                            mutpb=mutpb,
                                            indpb=indpb,
                                            output=self.output)
        if output:
            for result in results:
                self.output(_("参数：{}, 目标：{}").format(result[0], result[1]))
        return results

    def update_daily_close(self, price: float) -> None:
        dt = self.bar.datetime if self.mode == BacktestingMode.BAR else self.tick.datetime
        trading_date = self._get_trading_date(dt)

        daily_result: DailyResult | None = self.daily_results.get(trading_date, None)
        if daily_result:
            daily_result.close_price = price
        else:
            daily_result = DailyResult(trading_date, price)
            self.daily_results[trading_date] = daily_result

    def _do_rollover(self) -> None:
        if not self.routing_schedule:
            return

        check_dt = self.datetime.replace(second=0, microsecond=0) if self.mode == BacktestingMode.TICK else self.datetime
        best_symbol = self.routing_schedule.get(check_dt) or self.routing_schedule.get(check_dt.replace(microsecond=0))

        if not best_symbol:
            return

        try:
            if best_symbol != self.current_physical_symbol:
                if not self.current_physical_symbol:
                    # 冷启动：第一根 Bar，只记录符号不计费
                    self.current_physical_symbol = best_symbol
                    return

                if not self.strategy.trading:
                    # 非交易态（暖机期）只切符号不计费，记入 skip 账本
                    self.rollover_skip_logs.append({
                        "datetime": self.datetime,
                        "old_symbol": self.current_physical_symbol,
                        "new_symbol": best_symbol,
                        "reason": "strategy_not_trading"
                    })
                    self.current_physical_symbol = best_symbol
                    return

                if self.current_physical_symbol:
                    old_sym = self.current_physical_symbol
                    self.output(_("[{}] 📅 触发主力合约切换: {} -> {}").format(self.datetime, old_sym, best_symbol))

                    to_cancel_limit = [
                        vt_orderid for vt_orderid, order in self.active_limit_orders.items() if self._normalize_vt_symbol(
                            getattr(order, 'vt_symbol', f"{order.symbol}.{order.exchange.value}")) == old_sym
                    ]
                    for vt_orderid in to_cancel_limit:
                        self.cancel_limit_order(self.strategy, vt_orderid, reason="主力换月强制撤单", source="Rollover_Engine")

                    # 1. 强制撤销旧合约的停止单（规范化对比）
                    to_cancel_stop = [
                        soid for soid, stop_order in self.active_stop_orders.items()
                        if self._normalize_vt_symbol(stop_order.vt_symbol) == self._normalize_vt_symbol(old_sym)
                    ]
                    for soid in to_cancel_stop:
                        self.cancel_stop_order(self.strategy, soid, reason="主力换月强制撤单")

                    old_pos = self.physical_positions.pop(old_sym, 0)

                    if old_pos != 0:
                        old_bar = self.physical_bars.get((old_sym, check_dt))
                        new_bar = self.physical_bars.get((best_symbol, check_dt))

                        if old_bar:
                            ref_price = old_bar.close_price
                        elif new_bar:
                            ref_price = new_bar.close_price
                        elif self.mode == BacktestingMode.TICK and getattr(self, 'tick', None):
                            ref_price = self.tick.last_price
                        else:
                            ref_price = 0.0

                        if ref_price <= 0:
                            self.output(_("[{}] ⚠️ 换月时无有效物理价格，跳过手续费计算").format(self.datetime))
                            comm_cost = 0.0
                            slip_cost = 0.0
                        else:
                            # 🟢 【精细化修正】平旧开新分离构造 mock_trade，精准支持复杂阶梯费率模型
                            mock_trade_close = TradeData(symbol=old_sym.split(".")[0],
                                                         exchange=self.exchange,
                                                         orderid="ROLLOVER_CLOSE",
                                                         tradeid="ROLLOVER_CLOSE",
                                                         direction=Direction.LONG if old_pos < 0 else Direction.SHORT,
                                                         offset=Offset.CLOSE,
                                                         price=ref_price,
                                                         volume=abs(old_pos),
                                                         datetime=self.datetime,
                                                         gateway_name=self.gateway_name)
                            mock_trade_open = TradeData(symbol=best_symbol.split(".")[0],
                                                        exchange=self.exchange,
                                                        orderid="ROLLOVER_OPEN",
                                                        tradeid="ROLLOVER_OPEN",
                                                        direction=Direction.LONG if old_pos > 0 else Direction.SHORT,
                                                        offset=Offset.OPEN,
                                                        price=ref_price,
                                                        volume=abs(old_pos),
                                                        datetime=self.datetime,
                                                        gateway_name=self.gateway_name)
                            comm_cost = self.commission_model.get_commission(mock_trade_close, self.size) + \
                                        self.commission_model.get_commission(mock_trade_open, self.size)
                            slip_cost = self.slippage_model.get_slippage(mock_trade_close, self.size) + \
                                        self.slippage_model.get_slippage(mock_trade_open, self.size)

                        rollover_pnl = -slip_cost - comm_cost

                        self.rollover_logs.append({
                            "datetime": self.datetime,
                            "old_symbol": old_sym,
                            "new_symbol": best_symbol,
                            "position": old_pos,
                            "volume": abs(old_pos),
                            "direction": "Long" if old_pos > 0 else "Short",
                            "ref_price": ref_price,
                            "commission": comm_cost,
                            "slippage": slip_cost,
                            "rollover_pnl": rollover_pnl,
                            "reason": "main_contract_rollover",
                            "note": "仅计摩擦成本，新旧价差由底座复权拼接自然消化"
                        })

                        dt = self.bar.datetime if self.mode == BacktestingMode.BAR else self.tick.datetime
                        d = self._get_trading_date(dt)

                        daily_result: DailyResult | None = self.daily_results.get(d, None)
                        if not daily_result:
                            # 坚决只用 bar.close_price
                            fallback_price = self.bar.close_price if self.mode == BacktestingMode.BAR else self.tick.last_price
                            daily_result = DailyResult(d, fallback_price)
                            self.daily_results[d] = daily_result

                        daily_result.rollover_pnl += rollover_pnl

                        existing_pos = self.physical_positions.get(best_symbol, 0)
                        if existing_pos != 0:
                            self.output(
                                _("[{}] ⚠️ 警告: 换月时新主力 {} 已存在非零持仓 {}，执行累加合并").format(self.datetime, best_symbol, existing_pos))
                        self.physical_positions[best_symbol] = existing_pos + old_pos

                        self.output(_("[{}] 💰 执行换月仓位平移，产生纯摩擦损耗(滑点+手续费): {:.2f}").format(self.datetime, rollover_pnl))

                self.current_physical_symbol = best_symbol
        except Exception as e:
            self.rollover_logs.append({
                "datetime": self.datetime,
                "old_symbol": old_sym if 'old_sym' in dir() else self.current_physical_symbol,
                "new_symbol": best_symbol,
                "status": "FAILED",
                "error": str(e)
            })
            raise

    def new_bar(self, bar: BarData) -> None:
        self.bar = bar
        self.datetime = bar.datetime

        self._do_rollover()
        self.cross_limit_order()
        self.cross_stop_order()
        self.strategy.on_bar(bar)
        self.update_daily_close(bar.close_price)

    def new_tick(self, tick: TickData) -> None:
        self.tick = tick
        self.datetime = tick.datetime

        self._do_rollover()
        self.cross_limit_order()
        self.cross_stop_order()
        self.strategy.on_tick(tick)
        self.update_daily_close(tick.last_price)

    def cross_limit_order(self) -> None:
        if self.mode == BacktestingMode.BAR:
            if self.current_physical_symbol:
                lookup_dt = self.datetime.replace(microsecond=0)
                current_data = self.physical_bars.get((self.current_physical_symbol, lookup_dt))
                if not current_data:
                    return
            else:
                current_data = self.bar
        else:
            current_data = self.tick

        for order in list(self.active_limit_orders.values()):
            if order.vt_orderid not in self.active_limit_orders: continue

            # [修复] 初始状态流转也必须记入快照与隔离回调
            if order.status == Status.SUBMITTING:
                order.status = Status.NOTTRADED
                self._record_limit_order_history(order)
                self._call_strategy_on_order(order)

            trade_price, trade_volume = self.execution_model.match_limit_order(order, current_data)
            if trade_volume == 0: continue

            trade_volume = min(trade_volume, order.volume - order.traded)
            order.traded += trade_volume
            if order.traded == order.volume:
                order.status = Status.ALLTRADED
                self.active_limit_orders.pop(order.vt_orderid, None)
            else:
                order.status = Status.PARTTRADED

            self._record_limit_order_history(order)
            self._call_strategy_on_order(order)
            self.trade_count += 1

            vt_sym = self._normalize_vt_symbol(getattr(order, 'vt_symbol', f"{order.symbol}.{order.exchange.value}"))
            pos_change = trade_volume if order.direction == Direction.LONG else -trade_volume
            self.physical_positions[vt_sym] = self.physical_positions.get(vt_sym, 0) + pos_change

            trade: TradeData = TradeData(symbol=order.symbol,
                                         exchange=order.exchange,
                                         orderid=order.orderid,
                                         tradeid=str(self.trade_count),
                                         direction=order.direction,
                                         offset=order.offset,
                                         price=trade_price,
                                         volume=trade_volume,
                                         datetime=self.datetime,
                                         gateway_name=self.gateway_name)

            # [升维] 赋会计价
            order_offset = getattr(order, "price_offset", 0.0)
            trade.accounting_price = trade_price + order_offset
            trade.physical_price = trade_price
            trade.price_offset = order_offset

            self.strategy.pos += pos_change
            self._call_strategy_on_trade(trade)
            self.trades[trade.vt_tradeid] = trade

    def cross_stop_order(self) -> None:
        if self.mode == BacktestingMode.BAR:
            if self.current_physical_symbol:
                lookup_dt = self.datetime.replace(microsecond=0)
                current_data = self.physical_bars.get((self.current_physical_symbol, lookup_dt))
                if not current_data:
                    return
            else:
                current_data = self.bar
        else:
            current_data = self.tick

        for stop_order in list(self.active_stop_orders.values()):
            trade_price, trade_volume = self.execution_model.match_stop_order(stop_order, current_data)
            if trade_volume == 0: continue

            trade_volume = min(trade_volume, stop_order.volume)
            self.limit_order_count += 1
            target_vt_symbol = self._normalize_vt_symbol(stop_order.vt_symbol)
            target_symbol, target_exchange_str = target_vt_symbol.split(".")

            # 注：V1 止损单触发即全额成交，V2 部分成交模型需扩展此处状态机
            order: OrderData = OrderData(symbol=target_symbol,
                                         exchange=Exchange(target_exchange_str),
                                         orderid=str(self.limit_order_count),
                                         direction=stop_order.direction,
                                         offset=stop_order.offset,
                                         price=stop_order.price,
                                         volume=trade_volume,
                                         traded=trade_volume,
                                         status=Status.ALLTRADED,
                                         gateway_name=self.gateway_name,
                                         datetime=self.datetime)

            order_offset = getattr(stop_order, "price_offset", 0.0)
            order.accounting_price = getattr(stop_order, "accounting_price", stop_order.price)
            order.physical_price = stop_order.price
            order.price_offset = order_offset

            # [修复] 资金检查失败时的异常快照录入
            if not self.margin_model.check_margin(order, self):
                self.output(_("[{}] ⚠️ 止损单触发被拒：资金/保证金不足 - {}").format(self.datetime, order.vt_symbol))
                stop_order.status = StopOrderStatus.CANCELLED
                stop_order.cancel_reason = "资金/保证金不足"
                stop_order.cancel_datetime = self.datetime

                self._record_stop_order_history(stop_order)
                self._call_strategy_on_stop_order(stop_order)
                self.active_stop_orders.pop(stop_order.stop_orderid, None)
                continue

            self.limit_orders[order.vt_orderid] = order
            self._record_limit_order_history(order)  # 转化出来的限价单也要入账
            self.trade_count += 1

            vt_sym = self._normalize_vt_symbol(getattr(order, 'vt_symbol', f"{order.symbol}.{order.exchange.value}"))
            pos_change = trade_volume if order.direction == Direction.LONG else -trade_volume
            self.physical_positions[vt_sym] = self.physical_positions.get(vt_sym, 0) + pos_change

            trade: TradeData = TradeData(symbol=order.symbol,
                                         exchange=order.exchange,
                                         orderid=order.orderid,
                                         tradeid=str(self.trade_count),
                                         direction=order.direction,
                                         offset=order.offset,
                                         price=trade_price,
                                         volume=trade_volume,
                                         datetime=self.datetime,
                                         gateway_name=self.gateway_name)

            trade.accounting_price = trade_price + order_offset
            trade.physical_price = trade_price
            trade.price_offset = order_offset

            stop_order.vt_orderids.append(order.vt_orderid)
            stop_order.status = StopOrderStatus.TRIGGERED
            self._record_stop_order_history(stop_order)
            self.active_stop_orders.pop(stop_order.stop_orderid, None)

            self._call_strategy_on_stop_order(stop_order)
            self._call_strategy_on_order(order)
            self.strategy.pos += pos_change
            self._call_strategy_on_trade(trade)
            self.trades[trade.vt_tradeid] = trade

    def load_bar(self, vt_symbol: str, days: int, interval: Interval, callback: Callable, use_database: bool) -> list[BarData]:
        if self.physical_symbols:
            # 周期不匹配时强制对齐并记录审计日志
            if interval != self.interval:
                self.warmup_interval_mismatch_logs.append({"requested": interval.value, "engine": self.interval.value})
                self.output(f"⚠️ 策略请求 {interval.value} 周期，引擎为 {self.interval.value}。"
                            f"已强制对齐使用引擎周期暖机数据，请确认策略指标计算是否依赖绝对周期！")

            norm_start = self._normalize_datetime(self.start)
            target_start = norm_start - timedelta(days=days)

            # 时区安全切片
            warmup_bars = [b for b in self.warmup_data if target_start <= self._normalize_datetime(b.datetime) < norm_start]

            if not warmup_bars:
                self.output("❌ 警告：暖机数据提取为空！请检查数据源或加大 warmup_days")
            elif self._normalize_datetime(warmup_bars[0].datetime) > target_start:
                self.output(f"⚠️ 警告：暖机数据不足！请求从 {target_start} 开始，"
                            f"实际仅从 {self._normalize_datetime(warmup_bars[0].datetime)} 开始。")

            # 引擎只负责切片返回，CtaTemplate.load_bar 负责迭代 callback
            # 此时 strategy.trading == False，发单会被底层拦截
            return warmup_bars

        # 原生单合约查库逻辑
        self.callback = callback
        init_end = self.start - INTERVAL_DELTA_MAP[interval]
        init_start = self.start - timedelta(days=days)
        symbol, exchange = extract_vt_symbol(vt_symbol)
        return load_bar_data(symbol, exchange, interval, init_start, init_end)

    def load_tick(self, vt_symbol: str, days: int, callback: Callable) -> list[TickData]:
        self.callback = callback
        init_end = self.start - timedelta(seconds=1)
        init_start = self.start - timedelta(days=days)
        symbol, exchange = extract_vt_symbol(vt_symbol)
        return load_tick_data(symbol, exchange, init_start, init_end)

    def set_order_audit(self, order: OrderData, reason: str, source: str) -> None:
        """记录订单审计归因"""
        self.order_audit_logs[order.vt_orderid] = {
            "status": order.status.value,
            "status_reason": reason,
            "status_source": source,
            "status_datetime": self.datetime
        }

    def get_all_orders(self) -> list:
        """返回历史订单总账（含拒单、撤单、成交单）"""
        return list(self.limit_order_history.values())

    def get_order_audit_logs(self) -> dict:
        return dict(self.order_audit_logs)

    def send_order(self, strategy: CtaTemplate, direction: Direction, offset: Offset, price: float, volume: float, stop: bool,
                   lock: bool, net: bool) -> list:
        price = round_to(price, self.pricetick)
        if stop:
            vt_orderid = self.send_stop_order(direction, offset, price, volume)
        else:
            vt_orderid = self.send_limit_order(direction, offset, price, volume)

        # 🟢 【修复1】被拦截时抛出空列表，避免返回脏数据 [""]
        if not vt_orderid:
            return []
        return [vt_orderid]

    def send_stop_order(self, direction: Direction, offset: Offset, price: float, volume: float) -> str:
        # 防止暖机期产生幽灵止损单
        if not self.strategy.trading:
            phase = "on_start" if self.strategy.inited else "on_init"
            self.warmup_blocked_orders.append({
                "type": "stop",
                "datetime": self.datetime,
                "reason": "non_trading_blocked",
                "phase": phase
            })
            return ""

        self.stop_order_count += 1
        target_vt_symbol = self.current_physical_symbol if self.current_physical_symbol else self.vt_symbol
        target_vt_symbol = self._normalize_vt_symbol(target_vt_symbol)

        price_offset = self._get_current_price_offset()
        physical_price = round_to(price - price_offset, self.pricetick)

        # 实例化时直接用物理价
        stop_order: StopOrder = StopOrder(vt_symbol=target_vt_symbol,
                                          direction=direction,
                                          offset=offset,
                                          price=physical_price,
                                          volume=volume,
                                          datetime=self.datetime,
                                          stop_orderid=f"{STOPORDER_PREFIX}.{self.stop_order_count}",
                                          strategy_name=self.strategy.strategy_name)

        stop_order.physical_price = physical_price
        stop_order.accounting_price = price
        stop_order.price_offset = price_offset

        self.active_stop_orders[stop_order.stop_orderid] = stop_order
        self.stop_orders[stop_order.stop_orderid] = stop_order
        self._record_stop_order_history(stop_order)
        return stop_order.stop_orderid

    def cancel_limit_order(self,
                           strategy: CtaTemplate,
                           vt_orderid: str,
                           reason: str = "strategy_cancel",
                           source: str = "Strategy") -> None:
        if vt_orderid not in self.active_limit_orders: return
        order: OrderData = self.active_limit_orders.pop(vt_orderid)
        order.status = Status.CANCELLED
        self.limit_orders[vt_orderid] = order
        self._record_limit_order_history(order)  # 撤单快照
        self.set_order_audit(order, reason, source)
        self._call_strategy_on_order(order)

    def cancel_stop_order(self, strategy: CtaTemplate, vt_orderid: str, reason: str = "") -> None:
        if vt_orderid not in self.active_stop_orders: return
        stop_order: StopOrder = self.active_stop_orders.pop(vt_orderid)
        stop_order.status = StopOrderStatus.CANCELLED
        if reason: stop_order.cancel_reason = reason
        stop_order.cancel_datetime = self.datetime
        self._record_stop_order_history(stop_order)  # 撤单快照
        self._call_strategy_on_stop_order(stop_order)

    def send_limit_order(self, direction: Direction, offset: Offset, price: float, volume: float) -> str:
        # 利用原生 trading 状态拦截暖机期/on_start 期违规发单
        if not self.strategy.trading:
            phase = "on_start" if self.strategy.inited else "on_init"
            self.warmup_blocked_orders.append({
                "type": "limit",
                "datetime": self.datetime,
                "reason": "non_trading_blocked",
                "phase": phase
            })
            return ""

        self.limit_order_count += 1
        target_vt_symbol = self.current_physical_symbol if self.current_physical_symbol else self.vt_symbol
        target_vt_symbol = self._normalize_vt_symbol(target_vt_symbol)
        target_symbol, target_exchange_str = target_vt_symbol.split(".")

        price_offset = self._get_current_price_offset()
        physical_price = round_to(price - price_offset, self.pricetick)

        # [修复] 实例化时直接用物理价，防止旧价残留
        order: OrderData = OrderData(symbol=target_symbol,
                                     exchange=Exchange(target_exchange_str),
                                     orderid=str(self.limit_order_count),
                                     direction=direction,
                                     offset=offset,
                                     price=physical_price,
                                     volume=volume,
                                     status=Status.SUBMITTING,
                                     gateway_name=self.gateway_name,
                                     datetime=self.datetime)

        order.physical_price = physical_price
        order.accounting_price = price
        order.price_offset = price_offset
        self._record_limit_order_history(order)

        if not self.margin_model.check_margin(order, self):
            self.output(_("[{}] ⚠️ 订单被拒：资金/保证金不足 - {}").format(self.datetime, order.vt_symbol))
            order.status = Status.REJECTED
            self.limit_orders[order.vt_orderid] = order
            self._record_limit_order_history(order)  # 拒单快照
            self.set_order_audit(order, "资金/保证金不足", "Margin_Model")
            self._call_strategy_on_order(order)  # 隔离回调
            return ""

        self.active_limit_orders[order.vt_orderid] = order
        self.limit_orders[order.vt_orderid] = order
        return order.vt_orderid

    def cancel_order(self, strategy: CtaTemplate, vt_orderid: str) -> None:
        if vt_orderid.startswith(STOPORDER_PREFIX):
            self.cancel_stop_order(strategy, vt_orderid)
        else:
            self.cancel_limit_order(strategy, vt_orderid)

    def cancel_all(self, strategy: CtaTemplate) -> None:
        vt_orderids: list = list(self.active_limit_orders.keys())
        for vt_orderid in vt_orderids:
            self.cancel_limit_order(strategy, vt_orderid)
        stop_orderids: list = list(self.active_stop_orders.keys())
        for vt_orderid in stop_orderids:
            self.cancel_stop_order(strategy, vt_orderid)

    def write_log(self, msg: str, strategy: CtaTemplate | None = None) -> None:
        msg = f"{self.datetime}\t{msg}"
        self.logs.append(msg)

    def send_email(self, msg: str, strategy: CtaTemplate | None = None) -> None:
        pass

    def sync_strategy_data(self, strategy: CtaTemplate) -> None:
        pass

    def get_engine_type(self) -> EngineType:
        return self.engine_type

    def get_pricetick(self, strategy: CtaTemplate) -> float:
        return self.pricetick

    def get_size(self, strategy: CtaTemplate) -> float:
        return self.size

    def put_strategy_event(self, strategy: CtaTemplate) -> None:
        pass

    def output(self, msg: str) -> None:
        log_time = self.datetime if self.datetime.year > 1970 else datetime.now()
        print(f"{log_time}\t{msg}")

    def get_all_trades(self) -> list:
        return list(self.trades.values())

    def get_all_daily_results(self) -> list:
        return list(self.daily_results.values())

    def get_rollover_logs(self) -> list:
        return list(self.rollover_logs)

    def get_all_stop_orders(self) -> list:
        return list(self.stop_order_history.values())


class DailyResult:
    """"""

    def __init__(self, date: Date, close_price: float) -> None:
        self.date: Date = date
        self.close_price: float = close_price
        self.pre_close: float = 0

        self.trades: list[TradeData] = []
        self.trade_count: int = 0

        self.start_pos: float = 0
        self.end_pos: float = 0

        self.turnover: float = 0
        self.commission: float = 0
        self.slippage: float = 0
        self.rollover_pnl: float = 0.0

        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0
        self.net_pnl: float = 0

    def add_trade(self, trade: TradeData) -> None:
        self.trades.append(trade)

    def calculate_pnl(self, pre_close: float, start_pos: float, size: float, commission_model: BaseCommissionModel,
                      slippage_model: BaseSlippageModel) -> None:
        if pre_close:
            self.pre_close = pre_close
        else:
            self.pre_close = self.close_price

        self.start_pos = start_pos
        self.end_pos = start_pos

        self.holding_pnl = self.start_pos * (self.close_price - self.pre_close) * size
        self.trade_count = len(self.trades)

        for trade in self.trades:
            if trade.direction == Direction.LONG:
                pos_change = trade.volume
            else:
                pos_change = -trade.volume

            self.end_pos += pos_change

            turnover: float = trade.volume * size * trade.price
            self.slippage += slippage_model.get_slippage(trade, size)
            self.commission += commission_model.get_commission(trade, size)
            self.turnover += turnover

            # [绝对核心]：盯市必须用连续坐标系的 accounting_price
            acct_price = getattr(trade, 'accounting_price', trade.price)
            self.trading_pnl += pos_change * (self.close_price - acct_price) * size

        self.total_pnl = self.trading_pnl + self.holding_pnl
        self.net_pnl = self.total_pnl - self.commission - self.slippage + self.rollover_pnl


@lru_cache(maxsize=999)
def load_bar_data(symbol: str, exchange: Exchange, interval: Interval, start: datetime, end: datetime) -> list[BarData]:
    database: BaseDatabase = get_database()
    return database.load_bar_data(symbol, exchange, interval, start, end)


@lru_cache(maxsize=999)
def load_tick_data(symbol: str, exchange: Exchange, start: datetime, end: datetime) -> list[TickData]:
    database: BaseDatabase = get_database()
    return database.load_tick_data(symbol, exchange, start, end)


def evaluate(target_name: str, strategy_class: type[CtaTemplate], vt_symbol: str, interval: Interval, start: datetime,
             rate: float, slippage: float, size: float, pricetick: float, capital: int, end: datetime, mode: BacktestingMode,
             annual_days: int, half_life: int, physical_symbols: list, by_volume: bool, setting: dict) -> tuple:
    engine: BacktestingEngine = BacktestingEngine()
    engine.set_parameters(vt_symbol=vt_symbol,
                          interval=interval,
                          start=start,
                          rate=rate,
                          slippage=slippage,
                          size=size,
                          pricetick=pricetick,
                          capital=capital,
                          end=end,
                          mode=mode,
                          annual_days=annual_days,
                          half_life=half_life,
                          physical_symbols=physical_symbols,
                          by_volume=by_volume)
    engine.add_strategy(strategy_class, setting)
    engine.load_data()
    engine.run_backtesting()
    engine.calculate_result()
    statistics: dict = engine.calculate_statistics(output=False)
    target_value: float = statistics.get(target_name, 0)
    return (setting, target_value, statistics)


def wrap_evaluate(engine: BacktestingEngine, target_name: str) -> Callable:
    func: Callable = partial(evaluate, target_name, engine.strategy_class, engine.vt_symbol, engine.interval, engine.start,
                             engine.rate, engine.slippage, engine.size, engine.pricetick, engine.capital, engine.end,
                             engine.mode, engine.annual_days, engine.half_life, getattr(engine, 'physical_symbols', []),
                             getattr(engine, 'by_volume', False))
    return func


def calc_rgr_ratio(cagr_value: float, stability_return: float, annual_downside_risk: float, max_drawdown_percent: float,
                   return_skew: float, return_kurt: float, c_var: float) -> float:
    if cagr_value > 0:
        gain: float = np.log(1 + cagr_value)
    else:
        gain = -np.log(1 - cagr_value)
    skew_factor: float = 1 + 0.1 * np.tanh(return_skew)
    kurt_factor: float = 1 / (1 + 0.05 * max(return_kurt - 3, 0))
    downside_risk: float = max(annual_downside_risk, 1e-6)
    max_dd: float = abs(max_drawdown_percent) / 100.0
    if c_var != 0:
        cvar_risk: float = abs(c_var)
    else:
        cvar_risk = max_dd * 0.5
    combined_risk: float = 0.5 * downside_risk + 0.3 * max_dd + 0.2 * cvar_risk
    if combined_risk < 1e-9:
        combined_risk = 1e-9
    rgr_ratio: float = (gain * stability_return * skew_factor * kurt_factor) / combined_risk
    return rgr_ratio


def get_target_value(result: list | tuple) -> float:
    return cast(float, result[1])
