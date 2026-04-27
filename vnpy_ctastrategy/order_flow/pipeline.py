import uuid
from datetime import datetime
from vnpy.trader.constant import Direction, Offset
from vnpy.trader.object import ContractData
from .models import SignalOrder, OrderSource, RiskDecision
from .pipeline_stubs import DummyRiskManager, ExecutionAdapter
from .tracker import IntentTracker


class OrderPipeline:

    def __init__(self, tracker: IntentTracker):
        self.tracker = tracker
        self.risk_manager = DummyRiskManager(reject_keywords={"MALICIOUS_TEST"})
        self.execution_adapter = ExecutionAdapter()

    def process_signal(self,
                       strategy_name: str,
                       vt_symbol: str,
                       direction: Direction,
                       offset: Offset,
                       price: float,
                       volume: float,
                       lock: bool,
                       net: bool,
                       contract: ContractData,
                       created_at: datetime | None = None):
        signal = SignalOrder(chain_id=str(uuid.uuid4()),
                             source=OrderSource.STRATEGY,
                             strategy_name=strategy_name,
                             vt_symbol=vt_symbol,
                             direction=direction,
                             offset=offset,
                             price=price,
                             volume=volume,
                             lock=lock,
                             net=net,
                             created_at=created_at or datetime.now())
        self.tracker.init_chain(signal)

        risk_order = self.risk_manager.evaluate(signal)
        self.tracker.record_risk(risk_order)
        if risk_order.decision != RiskDecision.PASS:
            return signal, risk_order, None

        execution = self.execution_adapter.map_order(signal, contract)
        self.tracker.record_execution(execution)
        return signal, risk_order, execution
