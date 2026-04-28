import uuid
from datetime import datetime
from typing import Optional

from vnpy.trader.constant import Direction, Offset
from vnpy.trader.object import ContractData

from .models import SignalOrder, OrderSource, RiskDecision, MarketContext, AccountSnapshot
from .pipeline_stubs import DummyRiskManager, ExecutionAdapter
from .tracker import IntentTracker


class OrderPipeline:

    def __init__(self, tracker: IntentTracker):
        self.tracker = tracker
        self.risk_manager = DummyRiskManager(reject_keywords={"MALICIOUS_TEST"})
        self.execution_adapter = ExecutionAdapter()

    def process_signal(
        self,
        strategy_name: str,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        lock: bool,
        net: bool,
        contract: ContractData,
        created_at: datetime | None = None,
        # V1.5 新增：上下文感知型风控管理器所需参数，向下兼容（默认 None）
        context: Optional[MarketContext] = None,
        snapshot: Optional[AccountSnapshot] = None,
    ):
        signal = SignalOrder(
            chain_id=str(uuid.uuid4()),
            source=OrderSource.STRATEGY,
            strategy_name=strategy_name,
            vt_symbol=vt_symbol,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            lock=lock,
            net=net,
            created_at=created_at or datetime.now(),
        )
        self.tracker.init_chain(signal)

        # V1.5 核心修复：基于鸭子类型属性 requires_context 做分发
        # 取代脆弱的 inspect 字节码反射，同时对旧版 DummyRiskManager 完全透明
        if getattr(self.risk_manager, "requires_context", False):
            if context is None or snapshot is None:
                raise ValueError(
                    f"风控管理器 {type(self.risk_manager).__name__} 需要 context 和 snapshot，"
                    "但调用方未传入。请确认 send_order 已携带上下文参数。"
                )
            risk_order = self.risk_manager.evaluate(signal, context, snapshot, contract)
        else:
            # 旧版 DummyRiskManager 兜底路径，保持原有签名
            risk_order = self.risk_manager.evaluate(signal)

        self.tracker.record_risk(risk_order)

        # V1.5 致命级修复：精准截杀 REJECT，SHRINK 与 PASS 均放行
        if risk_order.decision == RiskDecision.REJECT:
            return signal, risk_order, None

        # 将风控裁剪后的手数传入执行适配器
        execution = self.execution_adapter.map_order(
            signal, contract, risk_order.adjusted_volume
        )
        self.tracker.record_execution(execution)
        return signal, risk_order, execution
