from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
from typing import Optional

from vnpy.trader.constant import Direction, Offset, Interval
from .locale import _

APP_NAME = "CtaStrategy"
STOPORDER_PREFIX = "STOP"


class StopOrderStatus(Enum):
    WAITING = _("等待中")
    CANCELLED = _("已撤销")
    TRIGGERED = _("已触发")


class EngineType(Enum):
    LIVE = _("实盘")
    BACKTESTING = _("回测")


class ExecutionProfile(Enum):
    LEGACY = "legacy"
    STANDARD = "standard"
    REALISTIC = "realistic"


class FrictionMode(Enum):
    LEGACY = "legacy"
    BAR_ENHANCED = "bar_enhanced"
    TICK_REPLAY = "tick_replay"


class BacktestingMode(Enum):
    BAR = 1
    TICK = 2


class TIF(Enum):
    """
    V1.6 订单有效期枚举（Time-In-Force）。
    - BAR_END : 信号 Bar 结束后未成交量自动撤销（默认）
    - GTC     : 直到明确撤单为止（Good Till Cancelled）
    止损触发后转入的 synthetic aggressive 订单默认 GTC，
    确保剩余量在后续 Tick 继续主动追成交，不因 BAR_END 到期而丢失。
    """
    BAR_END = "BAR_END"
    GTC = "GTC"


@dataclass
class StopOrder:
    vt_symbol: str
    direction: Direction
    offset: Offset
    price: float
    volume: float
    stop_orderid: str
    strategy_name: str
    datetime: datetime
    lock: bool = False
    net: bool = False
    vt_orderids: list = field(default_factory=list)
    status: StopOrderStatus = StopOrderStatus.WAITING
    cancel_reason: str = ""
    cancel_datetime: Optional[datetime] = None  # type: ignore
    #  用于报告明确展示容量裁剪余量作废的字段
    shrink_reason: str = ""
    original_volume: float = 0.0
    executed_volume: float = 0.0


EVENT_CTA_LOG = "eCtaLog"
EVENT_CTA_STRATEGY = "eCtaStrategy"
EVENT_CTA_STOPORDER = "eCtaStopOrder"

INTERVAL_DELTA_MAP: dict[Interval, timedelta] = {
    Interval.TICK: timedelta(milliseconds=1),
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}
