from abc import ABC, abstractmethod
from typing import Tuple, Union, Any

from vnpy.trader.object import OrderData, TradeData, BarData, TickData
from vnpy.trader.constant import Direction

from .base import StopOrder


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
    """V1 默认滑点模型：固定滑点跳数"""

    def __init__(self, slippage: float):
        self.slippage = slippage

    def get_slippage(self, trade: TradeData, size: float) -> float:
        return trade.volume * size * self.slippage


class V1DefaultCommissionModel(BaseCommissionModel):
    """V1 默认手续费模型：支持按比例(turnover)或按手数(volume)"""

    def __init__(self, rate: float, by_volume: bool = False):
        self.rate = rate
        self.by_volume = by_volume

    def get_commission(self, trade: TradeData, size: float) -> float:
        if self.by_volume:
            # 按手数收费 (例如：1.2元/手)
            return trade.volume * self.rate
        else:
            # 按成交额比例收费 (例如：万分之一)
            turnover = trade.volume * size * trade.price
            return turnover * self.rate


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
