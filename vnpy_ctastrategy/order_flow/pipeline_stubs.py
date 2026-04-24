from .models import RiskOrder, RiskDecision, ConstraintType, ExecutionOrder
from vnpy.trader.utility import round_to
import uuid


class DummyRiskManager:

    def __init__(self, reject_keywords: set = None):
        self.reject_keywords = reject_keywords or set()

    def evaluate(self, signal) -> RiskOrder:
        for keyword in self.reject_keywords:
            if keyword in signal.strategy_name:
                return RiskOrder(signal.chain_id, RiskDecision.REJECT, ConstraintType.HARD_LIMIT, f"测试风控拦截: 命中关键字 {keyword}")
        return RiskOrder(signal.chain_id, RiskDecision.PASS, ConstraintType.NONE)


class ExecutionAdapter:

    def __init__(self):
        pass

    def map_order(self, signal, contract) -> ExecutionOrder:
        rounded_price = round_to(signal.price, contract.pricetick)
        rounded_volume = round_to(signal.volume, contract.min_volume)
        return ExecutionOrder(chain_id=signal.chain_id,
                              exec_id=str(uuid.uuid4()),
                              direction=signal.direction,
                              offset=signal.offset,
                              lock=signal.lock,
                              net=signal.net,
                              raw_price=signal.price,
                              raw_volume=signal.volume,
                              pricetick=contract.pricetick,
                              min_volume=contract.min_volume,
                              rounded_price=rounded_price,
                              rounded_volume=rounded_volume)
