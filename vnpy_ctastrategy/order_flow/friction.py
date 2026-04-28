from dataclasses import dataclass
from enum import Enum


class MatchBehavior(Enum):
    PASSIVE_LIMIT = "PASSIVE_LIMIT"
    AGGRESSIVE_LIMIT = "AGGRESSIVE_LIMIT"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    MARKET_ORDER = "MARKET_ORDER"


@dataclass
class ExecutionMatchResult:
    matched: bool
    signal_price: float
    match_price: float
    volume: float
    behavior: MatchBehavior
    reason: str = ""


@dataclass
class SlippageResult:
    execution_price: float
    price_diff: float
    model_name: str


@dataclass
class CommissionResult:
    commission_amount: float
    model_name: str
