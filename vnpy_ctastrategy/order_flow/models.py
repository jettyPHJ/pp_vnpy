from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from vnpy.trader.constant import Direction, Offset


class OrderSource(Enum):
    STRATEGY = "STRATEGY"


class RiskDecision(Enum):
    PASS = "PASS"
    REJECT = "REJECT"


class ConstraintType(Enum):
    NONE = "NONE"
    HARD_LIMIT = "HARD_LIMIT"
    RATE_LIMIT = "RATE_LIMIT"


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


@dataclass
class RiskOrder:
    chain_id: str
    decision: RiskDecision
    constraint_type: ConstraintType
    reject_reason: str = ""
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
    created_at: datetime = field(default_factory=datetime.now)
