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
from vnpy.trader.object import ContractData
from .order_flow.tracker import IntentTracker
from .order_flow.position_ledger import PositionLedger
from .order_flow.pipeline import OrderPipeline
from .order_flow.models import RiskDecision
from vnpy.trader.optimize import (OptimizationSetting, check_optimization_setting, run_bf_optimization, run_ga_optimization)

from .base import (BacktestingMode, EngineType, STOPORDER_PREFIX, StopOrder, StopOrderStatus, INTERVAL_DELTA_MAP)
from .template import CtaTemplate
from .locale import _
from .continuous_builder import ContinuousBuilder

# 导入可热插拔的模块接口与 V1 默认实现
from .back_modules import (BaseMarginModel, BaseSlippageModel, BaseExecutionModel, BaseCommissionModel, V1DefaultMarginModel,
                           V1DefaultSlippageModel, V1DefaultExecutionModel, V1DefaultCommissionModel)

from vnpy_ctastrategy.base import ExecutionProfile
from vnpy_ctastrategy.back_modules import VolumeImpactSlippageModel
from vnpy_ctastrategy.order_flow.pipeline_stubs import CapitalAndSizeRiskManager, DummyRiskManager


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
        self.friction_mode: str = "legacy"
        self.trade_friction_map: dict[str, dict] = {}

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
        self.backtest_failed: bool = False
        self.backtest_error: str = ""
        self.intent_tracker = IntentTracker()
        self.position_ledger = PositionLedger()
        self.order_pipeline = OrderPipeline(self.intent_tracker)
        # 更新快捷引用
        self.actual_pos_map = self.position_ledger.actual_pos_map
        self.chain_audit_map = self.intent_tracker.chain_audit_map
        self.exempt_trade_records = self.intent_tracker.exempt_trade_records
        self.chain_audit_archive = self.intent_tracker.chain_audit_archive
        # V1.5 新增：滑动窗口缓存，用于 get_market_context 构造真实上下文
        from collections import deque
        self.vol_windows: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self.tr_windows: dict[str, deque] = defaultdict(lambda: deque(maxlen=14))
        self.last_closes: dict[str, float] = {}

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
        self.trade_friction_map.clear()
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
        self.order_audit_logs.clear()
        self.limit_order_history.clear()
        self.data_load_failed = False
        self.data_load_error = ""
        self.backtest_failed = False
        self.backtest_error = ""
        self.intent_tracker = IntentTracker()
        self.position_ledger = PositionLedger()
        self.order_pipeline = OrderPipeline(self.intent_tracker)
        # 更新快捷引用
        self.actual_pos_map = self.position_ledger.actual_pos_map
        self.chain_audit_map = self.intent_tracker.chain_audit_map
        self.exempt_trade_records = self.intent_tracker.exempt_trade_records
        self.chain_audit_archive = self.intent_tracker.chain_audit_archive
        # V1.5 重置滑动窗口（clear_data 后重新开始统计）
        from collections import deque
        self.vol_windows = defaultdict(lambda: deque(maxlen=20))
        self.tr_windows = defaultdict(lambda: deque(maxlen=14))
        self.last_closes = {}

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
        if not self.current_physical_symbol or not hasattr(self, "bar") or self.bar is None:
            return 0.0

        # 同样使用规范化的时间戳
        lookup_dt = self._normalize_lookup_dt(self.datetime)
        phys_bar = self.physical_bars.get((self.current_physical_symbol, lookup_dt))

        # 【警告】修复后此处将真正执行，若本地缺失部分物理 K 线数据，将引发 RuntimeError 阻断回测
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

    # ====== 检查验证 ======
    def _audit_financial_consistency(self) -> bool:
        """
        [内置财务审计] 执行双账本交叉核验 (Daily 账本 vs Rollover 独立日志)
        """
        self.output("🔎 正在执行交叉财务核验（Daily账本净值 vs 独立摩擦日志）...")

        # 1. 实际总净收益 (汇总 daily_results 中已经扣除过 rollover_pnl 的结果)
        actual_net_pnl = sum([result.net_pnl for result in self.daily_results.values()])

        # 2. 期望总净收益 (理论复权盈亏 - 交易手续费 - 交易滑点 + 独立换月日志中的摩擦之和)
        theoretical_pnl = sum([result.trading_pnl + result.holding_pnl for result in self.daily_results.values()])
        total_commission = sum([result.commission for result in self.daily_results.values()])
        total_slippage = sum([result.slippage for result in self.daily_results.values()])
        rollover_from_logs = sum([log.get("rollover_pnl", 0) for log in self.rollover_logs])

        if getattr(self, "friction_mode", "legacy") == "legacy":
            expected_net_pnl = theoretical_pnl - total_commission - total_slippage + rollover_from_logs
        else:
            expected_net_pnl = theoretical_pnl - total_commission + rollover_from_logs

        # 核心断言逻辑 (容忍极小的浮点数误差)
        tolerance = 1e-4
        is_consistent = abs(actual_net_pnl - expected_net_pnl) < tolerance

        if is_consistent:
            self.output("✅ 对账成功！换月损耗已正确汇入逐日结算账本。")
            return True
        else:
            self.output(f"❌ 严重警告：财务对账失败！实际总盈亏: {actual_net_pnl:.2f}, 期望总盈亏: {expected_net_pnl:.2f}")
            return False

    def _normalize_lookup_dt(self, dt: datetime, trim_second: bool = False) -> datetime:
        """
        统一处理查询字典用的时间戳，剥离时区并按需截断，确保与字典构建时的 key 严格一致。
        """
        if trim_second:
            dt = dt.replace(second=0, microsecond=0)
        else:
            dt = dt.replace(microsecond=0)
        return self._normalize_datetime(dt)

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
                       warmup_days: int = 120,
                       friction_mode: str = "legacy") -> None:
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
        self.friction_mode = friction_mode

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
            clean_dt = self._normalize_lookup_dt(b.datetime)

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

    def _normalize_vt_symbol(self, symbol: str) -> str:
        if "." in symbol:
            return symbol
        exchange = self.exchange.value if hasattr(self.exchange, "value") else str(self.exchange)
        return f"{symbol}.{exchange}"

    def _get_backtest_contract(self, vt_symbol: str) -> ContractData:
        symbol, exchange = extract_vt_symbol(vt_symbol)
        from vnpy.trader.constant import Product
        return ContractData(symbol=symbol,
                            exchange=exchange,
                            name=symbol,
                            product=Product.FUTURES,
                            size=self.size,
                            pricetick=self.pricetick,
                            min_volume=1,
                            gateway_name=self.gateway_name)

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
                    self.backtest_failed = True
                    self.backtest_error = traceback.format_exc()
                    self.output(_("触发异常，回测终止"))
                    self.output(self.backtest_error)
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
            daily_result.calculate_pnl(
                pre_close,
                start_pos,
                self.size,
                self.commission_model,
                self.slippage_model,
                friction_mode=self.friction_mode,
                trade_friction_map=self.trade_friction_map,
            )
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
                max_drawdown_duration = len(df.loc[max_drawdown_start:max_drawdown_end])
            else:
                max_drawdown_duration = 0

            total_net_pnl = df["net_pnl"].sum()
            daily_net_pnl = total_net_pnl / total_days
            total_commission = df["commission"].sum()
            daily_commission = total_commission / total_days
            total_slippage = df["slippage"].sum()
            daily_slippage = total_slippage / total_days

            # --- 【V1.2.1 新增】换月摩擦与综合(All-in)成本 ---
            # get("commission", 0) 完美兼容 FAILED 日志无该字段的情况
            total_rollover_commission = sum(log.get("commission", 0) for log in self.rollover_logs)
            total_rollover_slippage = sum(log.get("slippage", 0) for log in self.rollover_logs)
            all_in_commission = total_commission + total_rollover_commission
            all_in_slippage = total_slippage + total_rollover_slippage

            # 严格排除 FAILED 的失败记录
            rollover_count = sum(1 for log in self.rollover_logs if log.get("status") != "FAILED")

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
            if getattr(self, "friction_mode", "legacy") == "legacy":
                self.output(_("总滑点（独立扣减项）：\t{:,.2f}").format(total_slippage))
            else:
                self.output(_("总滑点（已内嵌至成交价）：\t{:,.2f}").format(total_slippage))
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

        # 统计完成后，自动执行底层的强制对账
        self._audit_financial_consistency()

        embedded_slippage_cost = total_slippage if getattr(self, "friction_mode", "legacy") == "v1.4" else 0.0
        deducted_slippage_cost = total_slippage if getattr(self, "friction_mode", "legacy") == "legacy" else 0.0

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
            "embedded_slippage_cost": embedded_slippage_cost,
            "deducted_slippage_cost": deducted_slippage_cost,
            "daily_slippage": daily_slippage,
            "total_turnover": total_turnover,
            "daily_turnover": daily_turnover,
            "total_trade_count": total_trade_count,
            "daily_trade_count": daily_trade_count,
            "rollover_count": rollover_count,
            "total_rollover_commission": total_rollover_commission,
            "total_rollover_slippage": total_rollover_slippage,
            "all_in_commission": all_in_commission,
            "all_in_slippage": all_in_slippage,
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
        # 强制剥离时区后再去 routing_schedule 和 physical_bars 查表
        check_dt = self._normalize_lookup_dt(self.datetime, trim_second=(self.mode == BacktestingMode.TICK))

        best_symbol = self.routing_schedule.get(check_dt)

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

    def configure_execution(
        self,
        profile: ExecutionProfile = ExecutionProfile.REALISTIC,
        margin_rate: float = 0.10,
        max_order_size: float = 50.0,
        max_participation_rate: float = 0.15,
        impact_factor: float = 1.5,
    ):
        """配置业务级执行环境"""
        if not getattr(self, "vt_symbol", ""):
            raise RuntimeError("⚠️ 引擎尚未初始化，请务必在 set_parameters() 之后调用 configure_execution()。")
        if not hasattr(self, "order_pipeline"):
            raise RuntimeError("⚠️ 审计管道未就绪，请检查 BacktestingEngine.__init__ 是否正确执行。")

        self.execution_profile = profile

        self.output("-" * 50)
        self.output(f"⚙️ Execution Profile: {profile.name}")

        if profile == ExecutionProfile.REALISTIC:
            self.friction_mode = "v1.4"
            self.slippage_model = VolumeImpactSlippageModel(impact_factor=impact_factor)
            self.order_pipeline.risk_manager = CapitalAndSizeRiskManager(margin_rate=margin_rate,
                                                                         max_order_size=max_order_size,
                                                                         max_participation_rate=max_participation_rate)
            self.output(f"   - Capital Constraint: Enabled ({margin_rate * 100}%)")
            self.output(f"   - Participation Limit: Enabled ({max_participation_rate * 100}%)")
            self.output(f"   - Dynamic Slippage: Enabled (Impact {impact_factor})")
            self.output(f"   - Stop Order Pipeline: Enabled (Strict Auditing)")

        elif profile == ExecutionProfile.STANDARD:
            self.friction_mode = "v1.4"
            self.order_pipeline.risk_manager = DummyRiskManager()
            self.output(f"   - Base Pipeline: Enabled (V1.4 Routing)")
            self.output(f"   - Risk/Capacity Constraints: Disabled")

        else:
            self.friction_mode = "legacy"
            self.order_pipeline.risk_manager = DummyRiskManager()
            self.output(f"   - Mode: Native unconstrained simulation")

        self.output("-" * 50)

    def new_bar(self, bar: BarData) -> None:
        self.bar = bar
        self.datetime = bar.datetime

        self._do_rollover()
        self.cross_limit_order()
        self.cross_stop_order()
        self.strategy.on_bar(bar)
        self.update_daily_close(bar.close_price)

        # ==============================================================
        # V1.5 修复：同时维护“连续合约”和“物理合约”的双轨容量观察窗口
        # ==============================================================
        vt = bar.vt_symbol
        self.vol_windows[vt].append(bar.volume)
        last_c = self.last_closes.get(vt)
        if last_c is not None:
            tr = max(
                bar.high_price - bar.low_price,
                abs(bar.high_price - last_c),
                abs(bar.low_price - last_c),
            )
            self.tr_windows[vt].append(tr)
        self.last_closes[vt] = bar.close_price

        # 同步当前主力物理合约的 K线数据到对应的滑动窗口
        if self.current_physical_symbol:
            lookup_dt = self._normalize_lookup_dt(self.datetime)
            phys_bar = self.physical_bars.get((self.current_physical_symbol, lookup_dt))
            if phys_bar:
                phys_vt = self.current_physical_symbol
                self.vol_windows[phys_vt].append(phys_bar.volume)
                last_phys_c = self.last_closes.get(phys_vt)
                if last_phys_c is not None:
                    tr_phys = max(
                        phys_bar.high_price - phys_bar.low_price,
                        abs(phys_bar.high_price - last_phys_c),
                        abs(phys_bar.low_price - last_phys_c),
                    )
                    self.tr_windows[phys_vt].append(tr_phys)
                self.last_closes[phys_vt] = phys_bar.close_price

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
            lookup_dt = self._normalize_lookup_dt(self.datetime)
            if self.current_physical_symbol:
                current_data = self.physical_bars.get((self.current_physical_symbol, lookup_dt))
                if not current_data:
                    return
            else:
                current_data = self.bar
        else:
            current_data = self.tick

        for order in list(self.active_limit_orders.values()):
            if order.vt_orderid not in self.active_limit_orders:
                continue

            # [修复] 初始状态流转也必须记入快照与隔离回调
            if order.status == Status.SUBMITTING:
                order.status = Status.NOTTRADED
                self._record_limit_order_history(order)
                self._call_strategy_on_order(order)

            if getattr(self, "friction_mode", "legacy") == "v1.4":
                match_res = self.execution_model.match_limit_order_v14(order, current_data)
                if not match_res.matched:
                    continue

                trade_volume = min(match_res.volume, order.volume - order.traded)
                order.traded += trade_volume
                if order.traded == order.volume:
                    order.status = Status.ALLTRADED
                    self.active_limit_orders.pop(order.vt_orderid, None)
                else:
                    order.status = Status.PARTTRADED

                self.intent_tracker.update_order(order)
                self._record_limit_order_history(order)
                self._call_strategy_on_order(order)
                self.trade_count += 1

                vt_sym = self._normalize_vt_symbol(getattr(order, 'vt_symbol', f"{order.symbol}.{order.exchange.value}"))
                pos_change = trade_volume if order.direction == Direction.LONG else -trade_volume
                self.physical_positions[vt_sym] = self.physical_positions.get(vt_sym, 0) + pos_change

                # V1.5：注入真实市场上下文，启用 calculate 统一接口
                _vt_sym = self._normalize_vt_symbol(getattr(order, 'vt_symbol', f"{order.symbol}.{order.exchange.value}"))
                _ctx = self.get_market_context(_vt_sym)
                slip_res = self.slippage_model.calculate(order, match_res, self.size, _ctx)
                trade: TradeData = TradeData(
                    symbol=order.symbol,
                    exchange=order.exchange,
                    orderid=order.orderid,
                    tradeid=str(self.trade_count),
                    direction=order.direction,
                    offset=order.offset,
                    price=slip_res.execution_price,
                    volume=trade_volume,
                    datetime=self.datetime,
                    gateway_name=self.gateway_name,
                )

                order_offset = getattr(order, "price_offset", 0.0)
                trade.accounting_price = slip_res.execution_price + order_offset
                trade.physical_price = slip_res.execution_price
                trade.price_offset = order_offset

                comm_res = self.commission_model.calculate_v14(trade, self.size)
                slippage_cost = abs(slip_res.price_diff) * trade_volume * self.size
                self.trade_friction_map[trade.vt_tradeid] = {
                    "slippage_cost": slippage_cost,
                    "commission_cost": comm_res.commission_amount,
                    "match_result": match_res,
                    "slippage_result": slip_res,
                    "commission_result": comm_res,
                }

                self.strategy.pos = self.position_ledger.apply_trade(trade)
                self._call_strategy_on_trade(trade)
                self.trades[trade.vt_tradeid] = trade
                self.intent_tracker.record_trade(
                    trade,
                    match_result=match_res,
                    slippage_result=slip_res,
                    commission_result=comm_res,
                    contract_multiplier=self.size,
                )
                continue

            trade_price, trade_volume = self.execution_model.match_limit_order(order, current_data)
            if trade_volume == 0:
                continue

            trade_volume = min(trade_volume, order.volume - order.traded)
            order.traded += trade_volume
            if order.traded == order.volume:
                order.status = Status.ALLTRADED
                self.active_limit_orders.pop(order.vt_orderid, None)
            else:
                order.status = Status.PARTTRADED

            self.intent_tracker.update_order(order)
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

            # 仓位先更新，确保策略拿到最新仓位
            self.strategy.pos = self.position_ledger.apply_trade(trade)

            # 回调策略
            self._call_strategy_on_trade(trade)

            # 落库并触发归档
            self.trades[trade.vt_tradeid] = trade
            self.intent_tracker.record_trade(trade)

    def cross_stop_order(self) -> None:
        if self.mode == BacktestingMode.BAR:
            lookup_dt = self._normalize_lookup_dt(self.datetime)
            if self.current_physical_symbol:
                current_data = self.physical_bars.get((self.current_physical_symbol, lookup_dt))
                if not current_data: return
            else:
                current_data = self.bar
        else:
            current_data = self.tick

        for stop_order in list(self.active_stop_orders.values()):

            if self.mode == BacktestingMode.BAR:
                long_cross = stop_order.direction == Direction.LONG and current_data.high_price >= stop_order.price
                short_cross = stop_order.direction == Direction.SHORT and current_data.low_price <= stop_order.price
            else:
                long_cross = stop_order.direction == Direction.LONG and current_data.last_price >= stop_order.price
                short_cross = stop_order.direction == Direction.SHORT and current_data.last_price <= stop_order.price

            if not (long_cross or short_cross):
                continue

            # =======================================================
            # 🛡️ V1.5 REALISTIC 硬核管控分支
            # =======================================================
            if getattr(self, "execution_profile", ExecutionProfile.LEGACY) == ExecutionProfile.REALISTIC:
                from vnpy_ctastrategy.order_flow.models import RiskDecision

                context = self.get_market_context(stop_order.vt_symbol)
                snapshot = self.get_account_snapshot()
                contract = self._get_backtest_contract(stop_order.vt_symbol)
                contract_multiplier = getattr(contract, "multiplier", getattr(contract, "size", self.size))

                signal, risk_order, execution = self.order_pipeline.process_signal(strategy_name=stop_order.strategy_name,
                                                                                   vt_symbol=stop_order.vt_symbol,
                                                                                   direction=stop_order.direction,
                                                                                   offset=stop_order.offset,
                                                                                   price=stop_order.price,
                                                                                   volume=stop_order.volume,
                                                                                   lock=stop_order.lock,
                                                                                   net=stop_order.net,
                                                                                   contract=contract,
                                                                                   created_at=self.datetime,
                                                                                   context=context,
                                                                                   snapshot=snapshot)
                signal.reference = f"STOP_TRIGGER_{stop_order.stop_orderid}"

                actual_volume = risk_order.adjusted_volume

                if risk_order.decision == RiskDecision.REJECT or actual_volume <= 0:
                    stop_order.status = StopOrderStatus.CANCELLED
                    stop_order.cancel_reason = risk_order.reject_reason or "资金/容量被风控硬拦截"
                    self.output(f"⚠️ [硬核风控] 止损单 {stop_order.stop_orderid} 触发被拦截: {stop_order.cancel_reason}")
                    self._record_stop_order_history(stop_order)
                    self._call_strategy_on_stop_order(stop_order)
                    self.active_stop_orders.pop(stop_order.stop_orderid)
                    continue

                if actual_volume < stop_order.volume:
                    self.output(f"⚠️ [容量裁剪] 止损单 {stop_order.stop_orderid} (目标 {stop_order.volume}) 仅成交 {actual_volume} 手，余量作废。")
                    stop_order.shrink_reason = "STOP_SHRINK_REMAINDER_DROPPED"
                    stop_order.original_volume = stop_order.volume
                    stop_order.executed_volume = actual_volume

                stop_order.status = StopOrderStatus.TRIGGERED

                self.limit_order_count += 1
                target_symbol, target_exchange_str = self._normalize_vt_symbol(stop_order.vt_symbol).split(".")

                order = OrderData(symbol=target_symbol,
                                  exchange=Exchange(target_exchange_str),
                                  orderid=str(self.limit_order_count),
                                  direction=stop_order.direction,
                                  offset=stop_order.offset,
                                  price=stop_order.price,
                                  volume=actual_volume,
                                  traded=actual_volume,
                                  status=Status.ALLTRADED,
                                  gateway_name=self.gateway_name,
                                  datetime=self.datetime)
                order_offset = getattr(stop_order, "price_offset", 0.0)
                order.accounting_price = stop_order.price
                order.physical_price = stop_order.price
                order.price_offset = order_offset

                self.limit_orders[order.vt_orderid] = order
                self._record_limit_order_history(order)

                self.intent_tracker.bind_order(order.vt_orderid, signal.chain_id, execution.exec_id, actual_volume)

                # 补全 physical_positions 的记录更新
                vt_sym = self._normalize_vt_symbol(getattr(order, 'vt_symbol', f"{order.symbol}.{order.exchange.value}"))
                pos_change = actual_volume if order.direction == Direction.LONG else -actual_volume
                self.physical_positions[vt_sym] = self.physical_positions.get(vt_sym, 0) + pos_change

                if self.mode == BacktestingMode.TICK:
                    match_price = current_data.last_price
                else:
                    match_price = max(stop_order.price, current_data.open_price) if long_cross else min(
                        stop_order.price, current_data.open_price)

                from vnpy_ctastrategy.order_flow.friction import ExecutionMatchResult, MatchBehavior
                match_res = ExecutionMatchResult(True, stop_order.price, match_price, actual_volume,
                                                 MatchBehavior.STOP_TRIGGERED)

                slip_res = self.slippage_model.calculate(signal, match_res, contract_multiplier, context)
                execution_price = slip_res.execution_price

                self.trade_count += 1
                trade = TradeData(symbol=order.symbol,
                                  exchange=order.exchange,
                                  orderid=order.orderid,
                                  tradeid=str(self.trade_count),
                                  direction=order.direction,
                                  offset=order.offset,
                                  price=execution_price,
                                  volume=actual_volume,
                                  datetime=self.datetime,
                                  gateway_name=self.gateway_name)
                trade.accounting_price = execution_price + order_offset
                trade.physical_price = execution_price
                trade.price_offset = order_offset

                comm_res = self.commission_model.calculate_v14(trade, contract_multiplier)

                self.trade_friction_map[trade.vt_tradeid] = {
                    "slippage_cost": abs(slip_res.price_diff) * actual_volume * contract_multiplier,
                    "commission_cost": comm_res.commission_amount,
                    "match_result": match_res,
                    "slippage_result": slip_res,
                    "commission_result": comm_res,
                }

                stop_order.vt_orderids.append(order.vt_orderid)
                self._record_stop_order_history(stop_order)
                self.active_stop_orders.pop(stop_order.stop_orderid, None)

                self._call_strategy_on_stop_order(stop_order)
                self._call_strategy_on_order(order)

                self.intent_tracker.record_trade(trade,
                                                 match_result=match_res,
                                                 slippage_result=slip_res,
                                                 commission_result=comm_res,
                                                 contract_multiplier=contract_multiplier)
                self.strategy.pos = self.position_ledger.apply_trade(trade)
                self._call_strategy_on_trade(trade)
                self.trades[trade.vt_tradeid] = trade

                continue

            # =======================================================
            # 🛡️ LEGACY / STANDARD 分支
            # =======================================================
            if getattr(self, "friction_mode", "legacy") == "v1.4":
                match_res = self.execution_model.match_stop_order_v14(stop_order, current_data)
                trade_price, trade_volume = match_res.match_price, match_res.volume
            else:
                match_res = None
                trade_price, trade_volume = self.execution_model.match_stop_order(stop_order, current_data)

            if trade_volume == 0:
                continue

            trade_volume = min(trade_volume, stop_order.volume)
            self.limit_order_count += 1
            target_vt_symbol = self._normalize_vt_symbol(stop_order.vt_symbol)
            target_symbol, target_exchange_str = target_vt_symbol.split(".")

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
            self._record_limit_order_history(order)
            self.trade_count += 1

            vt_sym = self._normalize_vt_symbol(getattr(order, 'vt_symbol', f"{order.symbol}.{order.exchange.value}"))
            pos_change = trade_volume if order.direction == Direction.LONG else -trade_volume
            self.physical_positions[vt_sym] = self.physical_positions.get(vt_sym, 0) + pos_change

            if getattr(self, "friction_mode", "legacy") == "v1.4":
                slip_res = self.slippage_model.calculate_v14(order, match_res, self.size)
                execution_price = slip_res.execution_price
            else:
                slip_res = None
                execution_price = trade_price

            trade: TradeData = TradeData(symbol=order.symbol,
                                         exchange=order.exchange,
                                         orderid=order.orderid,
                                         tradeid=str(self.trade_count),
                                         direction=order.direction,
                                         offset=order.offset,
                                         price=execution_price,
                                         volume=trade_volume,
                                         datetime=self.datetime,
                                         gateway_name=self.gateway_name)

            trade.accounting_price = execution_price + order_offset
            trade.physical_price = execution_price
            trade.price_offset = order_offset

            if getattr(self, "friction_mode", "legacy") == "v1.4":
                comm_res = self.commission_model.calculate_v14(trade, self.size)
                slippage_cost = abs(slip_res.price_diff) * trade_volume * self.size
                self.trade_friction_map[trade.vt_tradeid] = {
                    "slippage_cost": slippage_cost,
                    "commission_cost": comm_res.commission_amount,
                    "match_result": match_res,
                    "slippage_result": slip_res,
                    "commission_result": comm_res,
                }
            else:
                comm_res = None

            stop_order.vt_orderids.append(order.vt_orderid)
            stop_order.status = StopOrderStatus.TRIGGERED
            self._record_stop_order_history(stop_order)
            self.active_stop_orders.pop(stop_order.stop_orderid, None)

            self._call_strategy_on_stop_order(stop_order)
            self._call_strategy_on_order(order)

            if getattr(self, "friction_mode", "legacy") == "v1.4":
                self.intent_tracker.record_standalone_trade(
                    trade,
                    reason="STOP_ORDER_TRIGGERED",
                    match_result=match_res,
                    slippage_result=slip_res,
                    commission_result=comm_res,
                    contract_multiplier=self.size,
                )
            else:
                self.intent_tracker.mark_exempt(trade, reason="STOP_ORDER_TRIGGERED")

            self.strategy.pos = self.position_ledger.apply_trade(trade)
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
            if vt_orderid:
                self.output(f"[EXEMPT] 止损单跳过 V1.3 Pipeline: {vt_orderid}")
            return [vt_orderid] if vt_orderid else []

        target_vt_symbol = getattr(self, "current_physical_symbol", None) or self.vt_symbol
        target_vt_symbol = self._normalize_vt_symbol(target_vt_symbol)
        contract = self._get_backtest_contract(target_vt_symbol)

        # V1.5：抓取上下文与账户快照，供上下文感知型风控管理器使用
        context = self.get_market_context(target_vt_symbol)
        snapshot = self.get_account_snapshot()

        signal, risk_order, execution = self.order_pipeline.process_signal(
            strategy.strategy_name,
            target_vt_symbol,
            direction,
            offset,
            price,
            volume,
            lock,
            net,
            contract,
            created_at=self.datetime,
            context=context,
            snapshot=snapshot,
        )

        # V1.5 致命级修复：精准截杀 REJECT，SHRINK（手数裁剪）与 PASS 均放行
        if risk_order.decision == RiskDecision.REJECT:
            self.output(f"[RISK_REJECT] {signal.chain_id}: {risk_order.reject_reason}")
            return []

        vt_orderid = self.send_limit_order(execution.direction, execution.offset, execution.rounded_price,
                                           execution.rounded_volume)
        if vt_orderid:
            order = self.limit_orders.get(vt_orderid)
            self.intent_tracker.bind_order(vt_orderid, signal.chain_id, execution.exec_id, execution.rounded_volume)
            if order:
                self.intent_tracker.update_order(order)
            return [vt_orderid]
        return []

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
        self.intent_tracker.update_order(order)
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

    # ------------------------------------------------------------------
    # V1.5 新增：上下文与快照工厂方法，供 send_order 透传给流水线
    # ------------------------------------------------------------------

    def calculate_occupied_margin(self) -> float:
        """V1.5 MVP：暂不实现复杂资金占用，返回 0 保证回测不中断。"""
        return 0.0

    def get_market_context(self, vt_symbol: str):
        """
        构造当前 Bar 的市场上下文（V1.5 真实滑动窗口实现）。
        - is_ready=True：窗口填满后激活容量裁剪
        - is_ready=False：暖机期安全兜底，跳过容量裁剪
        """
        from .order_flow.models import MarketContext
        import numpy as np

        vol_q = self.vol_windows[vt_symbol]
        tr_q = self.tr_windows[vt_symbol]

        is_ready = (len(vol_q) == vol_q.maxlen) and (vol_q.maxlen > 0)
        ref_vol = float(np.mean(vol_q)) if vol_q else 1.0
        atr = float(np.mean(tr_q)) if tr_q else 0.0

        return MarketContext(
            vt_symbol=vt_symbol,
            current_atr=atr,
            reference_volume=ref_vol,
            is_ready=is_ready,
        )

    def get_account_snapshot(self):
        """构造当前账户快照，以初始资金扣除已占用保证金作为可用资金。"""
        from .order_flow.models import AccountSnapshot
        return AccountSnapshot(available_cash=self.capital - self.calculate_occupied_margin())


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

    def calculate_pnl(
        self,
        pre_close: float,
        start_pos: float,
        size: float,
        commission_model: BaseCommissionModel,
        slippage_model: BaseSlippageModel,
        friction_mode: str = "legacy",
        trade_friction_map: dict | None = None,
    ) -> None:
        if pre_close:
            self.pre_close = pre_close
        else:
            self.pre_close = self.close_price

        self.start_pos = start_pos
        self.end_pos = start_pos

        self.holding_pnl = self.start_pos * (self.close_price - self.pre_close) * size
        self.trade_count = len(self.trades)
        trade_friction_map = trade_friction_map or {}

        for trade in self.trades:
            if trade.direction == Direction.LONG:
                pos_change = trade.volume
            else:
                pos_change = -trade.volume

            self.end_pos += pos_change

            turnover: float = trade.volume * size * trade.price
            if friction_mode == "legacy":
                self.slippage += slippage_model.get_slippage(trade, size)
                self.commission += commission_model.get_commission(trade, size)
            else:
                friction_data = trade_friction_map.get(trade.vt_tradeid, {})
                self.commission += friction_data.get("commission_cost", commission_model.get_commission(trade, size))
                # V1.4 中该字段作为解释性统计，已内嵌在 trade.price，不参与 net_pnl 二次扣减。
                self.slippage += friction_data.get("slippage_cost", 0.0)
            self.turnover += turnover

            # [绝对核心]：盯市必须用连续坐标系的 accounting_price
            acct_price = getattr(trade, 'accounting_price', trade.price)
            self.trading_pnl += pos_change * (self.close_price - acct_price) * size

        self.total_pnl = self.trading_pnl + self.holding_pnl
        if friction_mode == "legacy":
            self.net_pnl = self.total_pnl - self.commission - self.slippage + self.rollover_pnl
        else:
            self.net_pnl = self.total_pnl - self.commission + self.rollover_pnl


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
             annual_days: int, half_life: int, physical_symbols: list, by_volume: bool, warmup_days: int,
             setting: dict) -> tuple:
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
                          by_volume=by_volume,
                          warmup_days=warmup_days)
    engine.add_strategy(strategy_class, setting)
    engine.load_data()
    engine.run_backtesting()

    # 短路拦截：如果这一组参数导致底层 RuntimeError（如缺 K 线），直接计 0 淘汰
    if getattr(engine, "backtest_failed", False) or getattr(engine, "data_load_failed", False):
        return (setting, 0.0, {})

    engine.calculate_result()
    statistics: dict = engine.calculate_statistics(output=False)
    target_value: float = statistics.get(target_name, 0)
    return (setting, target_value, statistics)


def wrap_evaluate(engine: BacktestingEngine, target_name: str) -> Callable:
    func: Callable = partial(evaluate, target_name, engine.strategy_class, engine.vt_symbol, engine.interval, engine.start,
                             engine.rate, engine.slippage, engine.size, engine.pricetick, engine.capital, engine.end,
                             engine.mode, engine.annual_days, engine.half_life, getattr(engine, 'physical_symbols', []),
                             getattr(engine, 'by_volume', False), getattr(engine, 'warmup_days', 120))
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
