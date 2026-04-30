from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MatchBehavior(Enum):
    PASSIVE_LIMIT = "PASSIVE_LIMIT"
    AGGRESSIVE_LIMIT = "AGGRESSIVE_LIMIT"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    MARKET_ORDER = "MARKET_ORDER"


class FillMode(Enum):
    """
    V1.6 Tick 撮合模式枚举。
    - TOP_OF_BOOK_CAPPED : 主动吃单，最多吃一档量（ask_vol_1 / bid_vol_1）
    - PASSIVE_TOUCH       : 被动挂单，按 delta_volume × participation_rate 成交
    - FULL_VOLUME         : 不做容量裁剪，适合压力测试 / 流动性充足场景
    """
    TOP_OF_BOOK_CAPPED = "TOP_OF_BOOK_CAPPED"
    PASSIVE_TOUCH = "PASSIVE_TOUCH"
    FULL_VOLUME = "FULL_VOLUME"


@dataclass
class ExecutionMatchResult:
    matched: bool
    signal_price: float
    match_price: float
    volume: float
    behavior: MatchBehavior
    reason: str = ""


@dataclass
class TickExecutionResult:
    """
    V1.6 Tick 撮合单步结果。
    - matched            : 是否触发成交
    - behavior           : 主动 / 被动 / 止损触发
    - match_price        : 本次成交价（盘口价，不含 Bar 滑点）
    - fill_volume        : 本次成交量
    - remaining_volume   : 剩余未成交量
    - mid_price          : 成交时刻中间价（用于偏离审计）
    - spread             : 成交时刻买卖价差
    - available_volume   : 一档可用量（用于调试 / 审计）
    """
    matched: bool
    behavior: MatchBehavior
    match_price: float
    fill_volume: float
    remaining_volume: float
    mid_price: float = 0.0
    spread: float = 0.0
    available_volume: float = 0.0


@dataclass
class SlippageResult:
    execution_price: float
    price_diff: float
    model_name: str


@dataclass
class CommissionResult:
    commission_amount: float
    model_name: str
