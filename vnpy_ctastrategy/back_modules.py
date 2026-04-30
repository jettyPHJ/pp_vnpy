from abc import ABC, abstractmethod
from typing import Tuple, Union, Any

from vnpy.trader.object import OrderData, TradeData, BarData, TickData
from vnpy.trader.constant import Direction

from .base import StopOrder
from .order_flow.friction import (ExecutionMatchResult, MatchBehavior, SlippageResult, CommissionResult, FillMode,
                                  TickExecutionResult)


class BaseMarginModel(ABC):
    """【资金与保证金模型】预留：用于未来实盘中因为资金不足导致废单的模拟"""

    @abstractmethod
    def check_margin(self, order: OrderData, engine: Any) -> bool:
        """
        检查是否有足够资金或保证金下单。
        :param engine: 传入 BacktestingEngine 实例，方便未来提取 engine.daily_df 或计算动态权益
        """
        pass


class BaseSlippageModel(ABC):
    """【动态滑点模型】预留：用于未来根据实盘的盘口波动、ATR、或排队位置，计算动态滑点"""

    @abstractmethod
    def get_slippage(self, trade: TradeData, size: float) -> float:
        """返回单笔成交产生的总滑点成本金额"""
        pass


class BaseExecutionModel(ABC):
    """【撮合与延迟模型】预留：用于未来处理实盘中的网络延迟、成交率不足(挂单吃不到量)、部分成交(Partial Fill)"""

    @abstractmethod
    def match_limit_order(self, order: OrderData, data_point: Union[BarData, TickData]) -> Tuple[float, float]:
        """返回: (trade_price, trade_volume) -> 如果未成交，返回 (0.0, 0.0)"""
        pass

    @abstractmethod
    def match_stop_order(self, stop_order: StopOrder, data_point: Union[BarData, TickData]) -> Tuple[float, float]:
        """返回: (trade_price, trade_volume) -> 如果未成交，返回 (0.0, 0.0)"""
        pass

    def match_limit_order_v14(self, order: OrderData, data_point: Union[BarData, TickData]) -> ExecutionMatchResult:
        """V1.4 兼容接口，默认由具体模型覆盖。"""
        raise NotImplementedError

    def match_stop_order_v14(self, stop_order: StopOrder, data_point: Union[BarData, TickData]) -> ExecutionMatchResult:
        """V1.4 兼容接口，默认由具体模型覆盖。"""
        raise NotImplementedError


class BaseCommissionModel(ABC):
    """【成本模型】预留：未来处理阶梯费率、平今仓惩罚费率等复杂结构"""

    @abstractmethod
    def get_commission(self, trade: TradeData, size: float) -> float:
        """返回单笔成交产生的总手续费"""
        pass


# ==========================================
# V1 默认基础实现
# ==========================================


class V1DefaultMarginModel(BaseMarginModel):
    """V1 默认资金模型：不卡资金，假设资金无限"""

    def check_margin(self, order: OrderData, engine: Any) -> bool:
        return True


class V1DefaultSlippageModel(BaseSlippageModel):
    """V1 默认滑点模型：固定绝对值滑点"""

    def __init__(self, slippage: float = 0.0):
        self.slippage = slippage

    def get_slippage(self, trade: TradeData, size: float) -> float:
        return trade.volume * size * self.slippage

    def calculate(self, order, match_result: ExecutionMatchResult, contract_multiplier: float, context=None) -> SlippageResult:
        """
        V1.5 新增：供新版撮合路径调用的统一接口，携带可选 context 参数预留扩展。
        被动限价单（PASSIVE_LIMIT）不产生额外滑点。
        """
        if match_result.behavior == MatchBehavior.PASSIVE_LIMIT:
            return SlippageResult(match_result.match_price, 0.0, "V1_Default_Slippage")

        exec_price = match_result.match_price + (self.slippage if order.direction == Direction.LONG else -self.slippage)
        return SlippageResult(exec_price, self.slippage, "V1_Default_Slippage")

    def calculate_v14(self, order: OrderData, match_result: ExecutionMatchResult, contract_multiplier: float) -> SlippageResult:
        """
        兼容保留：供 V1.4 遗留的 cross_limit_order 调用。
        待 V1.6 彻底重构撮合层时退役。
        """
        if match_result.behavior == MatchBehavior.PASSIVE_LIMIT:
            return SlippageResult(match_result.match_price, 0.0, "V1_Default_Slippage")

        execution_price = match_result.match_price + (self.slippage if order.direction == Direction.LONG else -self.slippage)
        return SlippageResult(
            execution_price=execution_price,
            price_diff=execution_price - match_result.match_price,
            model_name="V1_Default_Slippage",
        )


class FixedTickSlippageModel(BaseSlippageModel):
    """
    V1.5 新增：固定 N 跳滑点模型。
    滑点 = fixed_ticks × pricetick，方向感知。
    """

    def __init__(self, fixed_ticks: float = 1.0):
        self.fixed_ticks = fixed_ticks

    def get_slippage(self, trade: TradeData, size: float) -> float:
        # 此处 pricetick 信息需从外部传入，get_slippage 接口暂不支持；
        # 实际成本计算建议走 calculate / calculate_v14。
        return 0.0

    def calculate(self, order, match_result: ExecutionMatchResult, contract_multiplier: float, context=None) -> SlippageResult:
        if match_result.behavior == MatchBehavior.PASSIVE_LIMIT:
            return SlippageResult(match_result.match_price, 0.0, "FixedTick")

        # 安全防线：order 若为原生 OrderData 无 pricetick 属性，则回退到 1.0
        pricetick = getattr(order, "pricetick", 1.0)
        price_diff = self.fixed_ticks * pricetick
        exec_price = match_result.match_price + (price_diff if order.direction == Direction.LONG else -price_diff)
        return SlippageResult(exec_price, price_diff, "FixedTick")

    def calculate_v14(self, order: OrderData, match_result: ExecutionMatchResult, contract_multiplier: float) -> SlippageResult:
        """兼容 V1.4 撮合调用路径。"""
        return self.calculate(order, match_result, contract_multiplier)


class VolumeImpactSlippageModel(BaseSlippageModel):
    """
    V1.5 容量感知滑点：Almgren-Chriss 简化冲击模型。
    price_diff = impact_factor × sqrt(volume / reference_volume) × pricetick
    - reference_volume 来自 MarketContext（滑动均量），防止量纲失真
    - pricetick 确保冲击以价格单位计算
    - PASSIVE_LIMIT 不产生额外滑点
    """

    def __init__(self, impact_factor: float = 1.0):
        import math
        self._sqrt = math.sqrt
        self.impact_factor = impact_factor

    def get_slippage(self, trade: TradeData, size: float) -> float:
        # get_slippage 接口无法获取 reference_volume，不在此路径计算
        return 0.0

    def calculate(self, order, match_result: ExecutionMatchResult, contract_multiplier: float, context=None) -> SlippageResult:
        if match_result.behavior == MatchBehavior.PASSIVE_LIMIT:
            return SlippageResult(match_result.match_price, 0.0, "VolumeImpact")

        # 从上下文提取参考均量，防零除；无上下文时退化为 1 手均量
        ref_vol = max(context.reference_volume, 1e-8) if context else 1.0
        # 从 order 提取 pricetick，兼容 ExecutionOrder 与原生 OrderData
        pricetick = getattr(order, "pricetick", 1.0)

        price_diff = self.impact_factor * self._sqrt(match_result.volume / ref_vol) * pricetick
        exec_price = match_result.match_price + (price_diff if order.direction == Direction.LONG else -price_diff)
        return SlippageResult(exec_price, price_diff, "VolumeImpact")

    def calculate_v14(self, order: OrderData, match_result: ExecutionMatchResult, contract_multiplier: float) -> SlippageResult:
        # V1.4 兼容路径：无上下文，退化计算（量纲无法保证，仅用于遗留测试）
        return self.calculate(order, match_result, contract_multiplier, context=None)


class V1DefaultCommissionModel(BaseCommissionModel):
    """V1 默认手续费模型：支持按比例(turnover)或按手数(volume)"""

    def __init__(self, rate: float, by_volume: bool = False):
        self.rate = rate
        self.by_volume = by_volume

    def get_commission(self, trade: TradeData, size: float) -> float:
        if self.by_volume:
            return trade.volume * self.rate
        else:
            turnover = trade.volume * size * trade.price
            return turnover * self.rate

    def calculate_v14(self, trade: TradeData, contract_multiplier: float) -> CommissionResult:
        """V1.4: structured commission result shared by PnL and audit."""
        return CommissionResult(
            commission_amount=self.get_commission(trade, contract_multiplier),
            model_name="V1_Default_Commission",
        )


class V1DefaultExecutionModel(BaseExecutionModel):
    """V1 默认撮合模型：价格只要穿透，立刻全额成交，无延迟无容量限制"""

    def match_limit_order(self, order: OrderData, data_point: Union[BarData, TickData]) -> Tuple[float, float]:
        if isinstance(data_point, BarData):
            long_cross_price, short_cross_price = data_point.low_price, data_point.high_price
            long_best_price, short_best_price = data_point.open_price, data_point.open_price
        else:
            long_cross_price, short_cross_price = data_point.ask_price_1, data_point.bid_price_1
            long_best_price, short_best_price = long_cross_price, short_cross_price

        long_cross = (order.direction == Direction.LONG and order.price >= long_cross_price and long_cross_price > 0)
        short_cross = (order.direction == Direction.SHORT and order.price <= short_cross_price and short_cross_price > 0)

        if not long_cross and not short_cross:
            return 0.0, 0.0

        trade_price = min(order.price, long_best_price) if long_cross else max(order.price, short_best_price)
        return trade_price, order.volume

    def match_limit_order_v14(self, order: OrderData, data_point: Union[BarData, TickData]) -> ExecutionMatchResult:
        """V1.4: return explicit matching behavior for audit and friction models."""
        if isinstance(data_point, BarData):
            open_price = data_point.open_price
            low_price = data_point.low_price
            high_price = data_point.high_price
        else:
            open_price = low_price = high_price = data_point.last_price

        matched = False
        trade_price = 0.0
        behavior = MatchBehavior.PASSIVE_LIMIT

        if order.direction == Direction.LONG:
            if order.price >= open_price and open_price > 0:
                matched, trade_price, behavior = True, open_price, MatchBehavior.AGGRESSIVE_LIMIT
            elif order.price >= low_price and low_price > 0:
                matched, trade_price, behavior = True, order.price, MatchBehavior.PASSIVE_LIMIT
        else:
            if order.price <= open_price and open_price > 0:
                matched, trade_price, behavior = True, open_price, MatchBehavior.AGGRESSIVE_LIMIT
            elif order.price <= high_price and high_price > 0:
                matched, trade_price, behavior = True, order.price, MatchBehavior.PASSIVE_LIMIT

        if not matched:
            return ExecutionMatchResult(False, order.price, 0.0, 0.0, behavior)

        trade_volume = order.volume - order.traded
        return ExecutionMatchResult(True, order.price, trade_price, trade_volume, behavior)

    def match_stop_order_v14(self, stop_order: StopOrder, data_point: Union[BarData, TickData]) -> ExecutionMatchResult:
        """V1.4: reuse legacy stop trigger and tag STOP_TRIGGERED explicitly."""
        trade_price, trade_volume = self.match_stop_order(stop_order, data_point)
        if trade_volume == 0:
            return ExecutionMatchResult(False, stop_order.price, 0.0, 0.0, MatchBehavior.STOP_TRIGGERED)
        return ExecutionMatchResult(True, stop_order.price, trade_price, trade_volume, MatchBehavior.STOP_TRIGGERED)

    def match_stop_order(self, stop_order: StopOrder, data_point: Union[BarData, TickData]) -> Tuple[float, float]:
        if isinstance(data_point, BarData):
            long_cross_price, short_cross_price = data_point.high_price, data_point.low_price
            long_best_price, short_best_price = data_point.open_price, data_point.open_price
        else:
            long_cross_price, short_cross_price = data_point.last_price, data_point.last_price
            long_best_price, short_best_price = long_cross_price, short_cross_price

        long_cross = (stop_order.direction == Direction.LONG and stop_order.price <= long_cross_price)
        short_cross = (stop_order.direction == Direction.SHORT and stop_order.price >= short_cross_price)

        if not long_cross and not short_cross:
            return 0.0, 0.0

        trade_price = max(stop_order.price, long_best_price) if long_cross else min(stop_order.price, short_best_price)
        return trade_price, stop_order.volume


# ==========================================
# V1.6 Tick 撮合模型
# ==========================================


class TickExecutionModel:
    """
    V1.6 Tick 级撮合模型。
    - match()      : 撮合限价单，返回 TickExecutionResult（不含 Bar slippage）
    - match_stop() : 撮合止损单，返回 TickExecutionResult

    执行价直接取盘口价，不二次叠加 Bar slippage model。
    成本归因：execution_price - mid_price 记录在审计字段，而非额外滑点。
    """

    def match(
        self,
        order: OrderData,
        ctx,  # TickExecutionContext
        fill_mode: FillMode,
        participation_rate: float = 0.3,
    ) -> TickExecutionResult:
        """
        撮合限价单。
        主动成交条件（AGGRESSIVE）：买单 price >= ask1；卖单 price <= bid1。
        被动成交条件（PASSIVE）：买单 last_price <= order.price；卖单 last_price >= order.price。
        """
        remaining = order.volume - order.traded
        no_fill = TickExecutionResult(
            matched=False,
            behavior=MatchBehavior.AGGRESSIVE_LIMIT,
            match_price=0.0,
            fill_volume=0.0,
            remaining_volume=remaining,
            mid_price=ctx.mid_price,
            spread=ctx.spread,
        )

        if remaining <= 0:
            return no_fill

        is_long = order.direction == Direction.LONG

        # synthetic aggressive 单：止损触发后的剩余量，始终按当前对手盘主动撮合，不再受原 order.price 限制。
        if getattr(order, "synthetic_aggressive", False):
            if is_long:
                match_price = ctx.ask1 if ctx.ask1 > 0 else ctx.last_price
                available_vol = ctx.ask_vol_1
            else:
                match_price = ctx.bid1 if ctx.bid1 > 0 else ctx.last_price
                available_vol = ctx.bid_vol_1

            if match_price <= 0 or available_vol <= 0:
                return no_fill

            fill_vol = remaining if fill_mode == FillMode.FULL_VOLUME else min(remaining, max(available_vol, 0.0))
            if fill_vol <= 0:
                return no_fill

            return TickExecutionResult(
                matched=True,
                behavior=MatchBehavior.AGGRESSIVE_LIMIT,
                match_price=match_price,
                fill_volume=fill_vol,
                remaining_volume=max(remaining - fill_vol, 0.0),
                mid_price=ctx.mid_price,
                spread=ctx.spread,
                available_volume=available_vol,
            )

        # ------------------------------------------------------------------
        # 主动成交路径（order price 穿越对手盘一档）
        # ------------------------------------------------------------------
        if is_long and ctx.ask1 > 0 and order.price >= ctx.ask1:
            match_price = ctx.ask1
            available_vol = ctx.ask_vol_1
            behavior = MatchBehavior.AGGRESSIVE_LIMIT

        elif not is_long and ctx.bid1 > 0 and order.price <= ctx.bid1:
            match_price = ctx.bid1
            available_vol = ctx.bid_vol_1
            behavior = MatchBehavior.AGGRESSIVE_LIMIT

        # ------------------------------------------------------------------
        # 被动成交路径（last_price 触及挂单价）
        # ------------------------------------------------------------------
        elif is_long and ctx.last_price > 0 and ctx.last_price <= order.price:
            match_price = order.price
            available_vol = ctx.delta_volume
            behavior = MatchBehavior.PASSIVE_LIMIT

        elif not is_long and ctx.last_price > 0 and ctx.last_price >= order.price:
            match_price = order.price
            available_vol = ctx.delta_volume
            behavior = MatchBehavior.PASSIVE_LIMIT

        else:
            return no_fill

        # ------------------------------------------------------------------
        # 容量裁剪
        # ------------------------------------------------------------------
        if fill_mode == FillMode.FULL_VOLUME:
            fill_vol = remaining
        elif behavior == MatchBehavior.PASSIVE_LIMIT:
            # 被动单永远按区间参与率裁剪，不受 FillMode 影响。
            fill_vol = min(remaining, max(available_vol * participation_rate, 0.0))
        else:
            # 主动单默认按一档可见量裁剪。
            fill_vol = min(remaining, max(available_vol, 0.0))

        if fill_vol <= 0:
            return no_fill

        return TickExecutionResult(
            matched=True,
            behavior=behavior,
            match_price=match_price,
            fill_volume=fill_vol,
            remaining_volume=max(remaining - fill_vol, 0.0),
            mid_price=ctx.mid_price,
            spread=ctx.spread,
            available_volume=available_vol,
        )

    def match_stop(
        self,
        stop_order,  # StopOrder
        ctx,  # TickExecutionContext
        fill_mode: FillMode,
        participation_rate: float = 0.3,
    ) -> TickExecutionResult:
        """
        撮合止损单。
        触发条件：
        - 买入止损：last_price >= stop_order.price
        - 卖出止损：last_price <= stop_order.price
        触发后以对手盘一档价成交（最差价保护）。
        """
        remaining = stop_order.volume
        no_fill = TickExecutionResult(
            matched=False,
            behavior=MatchBehavior.STOP_TRIGGERED,
            match_price=0.0,
            fill_volume=0.0,
            remaining_volume=remaining,
            mid_price=ctx.mid_price,
            spread=ctx.spread,
        )

        is_long = stop_order.direction == Direction.LONG
        triggered = ((is_long and ctx.last_price > 0 and ctx.last_price >= stop_order.price)
                     or (not is_long and ctx.last_price > 0 and ctx.last_price <= stop_order.price))
        if not triggered:
            return no_fill

        # 触发后吃对手盘一档
        if is_long:
            match_price = ctx.ask1 if ctx.ask1 > 0 else ctx.last_price
            available_vol = ctx.ask_vol_1
        else:
            match_price = ctx.bid1 if ctx.bid1 > 0 else ctx.last_price
            available_vol = ctx.bid_vol_1

        if fill_mode == FillMode.FULL_VOLUME:
            fill_vol = remaining
        else:
            fill_vol = min(remaining, max(available_vol, 0.0))

        if fill_vol <= 0:
            # 触发但盘口无量，记录触发不成交
            return TickExecutionResult(
                matched=True,
                behavior=MatchBehavior.STOP_TRIGGERED,
                match_price=match_price,
                fill_volume=0.0,
                remaining_volume=remaining,
                mid_price=ctx.mid_price,
                spread=ctx.spread,
                available_volume=available_vol,
            )

        return TickExecutionResult(
            matched=True,
            behavior=MatchBehavior.STOP_TRIGGERED,
            match_price=match_price,
            fill_volume=fill_vol,
            remaining_volume=max(remaining - fill_vol, 0.0),
            mid_price=ctx.mid_price,
            spread=ctx.spread,
            available_volume=available_vol,
        )
