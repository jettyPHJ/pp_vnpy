import math
import uuid

from vnpy.trader.utility import round_to

from .models import (
    RiskOrder, RiskDecision, ConstraintType,
    ExecutionOrder, MarketContext, AccountSnapshot,
)


class DummyRiskManager:
    """存量旧版风控，用于测试与兜底。无需上下文。"""

    requires_context = False   # V1.5 鸭子类型标识，流水线据此做分发

    def __init__(self, reject_keywords: set = None):
        self.reject_keywords = reject_keywords or set()

    def evaluate(self, signal) -> RiskOrder:
        for keyword in self.reject_keywords:
            if keyword in signal.strategy_name:
                return RiskOrder(
                    chain_id=signal.chain_id,
                    decision=RiskDecision.REJECT,
                    constraint_type=ConstraintType.HARD_LIMIT,
                    reject_reason=f"测试风控拦截: 命中关键字 {keyword}",
                    original_volume=signal.volume,
                    adjusted_volume=0.0,
                )

        # V1.5 核心修复：PASS 时必须显式带回原始手数，防止下游取 None 静默失效
        return RiskOrder(
            chain_id=signal.chain_id,
            decision=RiskDecision.PASS,
            constraint_type=ConstraintType.NONE,
            reject_reason="",
            original_volume=signal.volume,
            adjusted_volume=signal.volume,
        )


class CapitalAndSizeRiskManager:
    """V1.5 新增：资金与容量联合风控，需要市场上下文与账户快照。"""

    requires_context = True    # V1.5 鸭子类型标识

    def __init__(
        self,
        margin_rate: float = 0.1,
        max_order_size: float = float("inf"),
        max_participation_rate: float = 1.0,
    ):
        self.margin_rate = margin_rate
        self.max_order_size = max_order_size
        self.max_participation_rate = max_participation_rate

    def evaluate(
        self,
        signal,
        context: MarketContext,
        snapshot: AccountSnapshot,
        contract,
    ) -> RiskOrder:
        adjusted_vol = signal.volume
        reason = ""
        decision = RiskDecision.PASS
        constraint = ConstraintType.NONE

        # 容量裁剪：仅在上下文就绪（暖机期结束）时生效
        if context.is_ready:
            vol_cap = context.reference_volume * self.max_participation_rate
            if adjusted_vol > self.max_order_size or adjusted_vol > vol_cap:
                adjusted_vol = min(adjusted_vol, self.max_order_size, vol_cap)
                decision = RiskDecision.SHRINK
                constraint = ConstraintType.SIZE
                reason = "触发容量或单笔上限裁剪"

        # 安全防线：向下取整到最小变动手数，避免 0 手静默失效
        min_vol = getattr(contract, "min_volume", 1.0)
        adjusted_vol = math.floor(adjusted_vol / min_vol) * min_vol

        if adjusted_vol <= 0:
            return RiskOrder(
                chain_id=signal.chain_id,
                decision=RiskDecision.REJECT,
                constraint_type=ConstraintType.SIZE,
                reject_reason="手数被取整为 0",
                original_volume=signal.volume,
                adjusted_volume=0.0,
            )

        # 保证金检查
        multiplier = getattr(contract, "size", getattr(contract, "multiplier", 1.0))
        req_margin = adjusted_vol * signal.price * multiplier * self.margin_rate
        if req_margin > snapshot.available_cash:
            return RiskOrder(
                chain_id=signal.chain_id,
                decision=RiskDecision.REJECT,
                constraint_type=ConstraintType.CAPITAL,
                reject_reason="可用资金不足",
                original_volume=signal.volume,
                adjusted_volume=0.0,
            )

        return RiskOrder(
            chain_id=signal.chain_id,
            decision=decision,
            constraint_type=constraint,
            reject_reason=reason,
            original_volume=signal.volume,
            adjusted_volume=adjusted_vol,
        )


class ExecutionAdapter:

    def __init__(self):
        pass

    def map_order(self, signal, contract, adjusted_volume: float = None) -> ExecutionOrder:
        # V1.5：优先使用风控裁剪后的手数；None 哨兵兜底回退到信号原始手数
        vol = adjusted_volume if adjusted_volume is not None else signal.volume

        rounded_price = round_to(signal.price, contract.pricetick)
        rounded_volume = round_to(vol, contract.min_volume)

        return ExecutionOrder(
            chain_id=signal.chain_id,
            exec_id=str(uuid.uuid4()),
            direction=signal.direction,
            offset=signal.offset,
            lock=signal.lock,
            net=signal.net,
            raw_price=signal.price,
            raw_volume=vol,
            pricetick=contract.pricetick,
            min_volume=contract.min_volume,
            rounded_price=rounded_price,
            rounded_volume=rounded_volume,
        )
