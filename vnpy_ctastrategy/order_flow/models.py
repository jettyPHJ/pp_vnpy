from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from vnpy.trader.constant import Direction, Offset


class OrderSource(Enum):
    STRATEGY = "STRATEGY"


class RiskDecision(Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    SHRINK = "SHRINK"  # V1.5 新增：风控裁剪后放行


class ConstraintType(Enum):
    NONE = "NONE"
    HARD_LIMIT = "HARD_LIMIT"
    RATE_LIMIT = "RATE_LIMIT"
    CAPITAL = "CAPITAL"  # V1.5 新增：资金不足
    SIZE = "SIZE"  # V1.5 新增：手数裁剪/取整为0


# ---------------------------------------------------------------------------
# V1.5 新增：市场上下文与账户快照，供上下文感知型风控管理器使用
# ---------------------------------------------------------------------------


@dataclass
class MarketContext:
    vt_symbol: str
    current_atr: float = 0.0
    reference_volume: float = 0.0
    is_ready: bool = False  # False 时跳过容量裁剪，避免暖机期误杀


@dataclass
class AccountSnapshot:
    available_cash: float


# ---------------------------------------------------------------------------
# 核心数据模型
# ---------------------------------------------------------------------------


@dataclass
class SignalOrder:
    chain_id: str
    source: OrderSource
    strategy_name: str
    vt_symbol: str
    direction: Direction
    offset: Offset
    price: float
    volume: float
    lock: bool
    net: bool
    created_at: datetime = field(default_factory=datetime.now)
    reference: str = ""


@dataclass
class RiskOrder:
    chain_id: str
    decision: RiskDecision
    constraint_type: ConstraintType
    reject_reason: str = ""
    # V1.5 新增：使用 Optional + None 哨兵，防止下游取 0 造成静默失效
    original_volume: Optional[float] = None
    adjusted_volume: Optional[float] = None
    processed_at: datetime = field(default_factory=datetime.now)


@dataclass
class ExecutionOrder:
    chain_id: str
    exec_id: str
    direction: Direction
    offset: Offset
    lock: bool
    net: bool
    raw_price: float
    raw_volume: float
    pricetick: float
    min_volume: float
    rounded_price: float
    rounded_volume: float
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class PhysicalOrderRef:
    """物理报单映射，解决 1 Signal -> N vt_orderid 的断层"""
    vt_orderid: str
    chain_id: str
    exec_id: str
    status: object = None
    volume: float = 0
    traded: float = 0
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = None
