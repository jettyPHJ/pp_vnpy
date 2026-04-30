import math
from datetime import datetime
from vnpy.trader.constant import Status
from vnpy.trader.object import OrderData, TradeData
from .models import SignalOrder, RiskOrder, ExecutionOrder, PhysicalOrderRef

TERMINAL_STATUSES = {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}


class IntentTracker:

    def __init__(self) -> None:
        self.orderid_chain_map: dict[str, str] = {}
        self.chain_audit_map: dict[str, dict] = {}
        self.chain_audit_archive: list[dict] = []

        self.exempt_tradeids: set = set()
        self.exempt_trade_records: list[dict] = []  # 存 dict，避免往 trade 挂动态属性
        self.untracked_trade_count: int = 0

    def init_chain(self, signal: SignalOrder) -> None:
        self.chain_audit_map[signal.chain_id] = {
            "signal": signal,
            "risk": None,
            "executions": [],
            "orders": [],
            "trades": [],
            "cancellations": [],  # V1.6：BAR_END / 手动撤单的审计记录
            "created_at": signal.created_at
        }

    def record_risk(self, risk: RiskOrder) -> None:
        if risk.chain_id in self.chain_audit_map:
            self.chain_audit_map[risk.chain_id]["risk"] = risk
            if risk.decision.value == "REJECT":  # 拒单立即触发归档
                self.try_archive(risk.chain_id)

    def record_execution(self, execution: ExecutionOrder) -> None:
        if execution.chain_id in self.chain_audit_map:
            self.chain_audit_map[execution.chain_id]["executions"].append(execution)

    def bind_order(self, vt_orderid: str, chain_id: str, exec_id: str, volume: float) -> None:
        ref = PhysicalOrderRef(vt_orderid=vt_orderid, chain_id=chain_id, exec_id=exec_id, volume=volume)
        if chain_id in self.chain_audit_map:
            self.chain_audit_map[chain_id]["orders"].append(ref)
        self.orderid_chain_map[vt_orderid] = chain_id

    def update_order(self, order: OrderData) -> None:
        chain_id = self.orderid_chain_map.get(order.vt_orderid)
        if not chain_id or chain_id not in self.chain_audit_map:
            return

        for ref in self.chain_audit_map[chain_id]["orders"]:
            if ref.vt_orderid == order.vt_orderid:
                ref.status = order.status
                ref.traded = order.traded
                ref.updated_at = order.datetime or datetime.now()
                break
        self.try_archive(chain_id)

    def record_trade(
        self,
        trade: TradeData,
        match_result=None,
        slippage_result=None,
        commission_result=None,
        contract_multiplier: float = 1.0,
        # V1.6 Tick 撮合专用审计字段
        tick_fill_mode=None,
        tick_fill_volume: float = None,
        tick_remaining: float = None,
        tick_mid_price: float = None,
        tick_mid_offset: float = None,
    ) -> None:
        if trade.vt_tradeid in self.exempt_tradeids:
            return

        chain_id = self.orderid_chain_map.get(trade.vt_orderid)
        if not chain_id or chain_id not in self.chain_audit_map:
            self.untracked_trade_count += 1
            return

        trade_record = {
            "trade": trade,
            "match_result": match_result,
            "slippage_result": slippage_result,
            "commission_result": commission_result,
            "contract_multiplier": contract_multiplier,
            # V1.6 Tick 审计字段（Bar 路径均为 None，便于 validator 区分）
            "tick_fill_mode": tick_fill_mode,
            "tick_fill_volume": tick_fill_volume,
            "tick_remaining": tick_remaining,
            "tick_mid_price": tick_mid_price,
            "tick_mid_offset": tick_mid_offset,
        }
        self.chain_audit_map[chain_id]["trades"].append(trade_record)
        self.try_archive(chain_id)

    def record_cancellation(
        self,
        order,
        reason: str = "",
        remaining_volume: float = 0.0,
        cancelled_at=None,
    ) -> None:
        """
        V1.6：记录撤单事件到对应 chain 的 cancellations 列表。
        reason 统一使用常量字符串，例如 "TIF_BAR_END_EXPIRED"。
        """
        chain_id = self.orderid_chain_map.get(getattr(order, "vt_orderid", ""))
        if not chain_id or chain_id not in self.chain_audit_map:
            return
        self.chain_audit_map[chain_id]["cancellations"].append({
            "order": order,
            "reason": reason,
            "remaining_volume": remaining_volume,
            "cancelled_at": cancelled_at or datetime.now(),
        })
        self.try_archive(chain_id)

    def record_standalone_trade(
        self,
        trade: TradeData,
        reason: str = "STOP_ORDER",
        match_result=None,
        slippage_result=None,
        commission_result=None,
        contract_multiplier: float = 1.0,
    ) -> None:
        self.exempt_tradeids.add(trade.vt_tradeid)
        self.exempt_trade_records.append({
            "trade": trade,
            "reason": reason,
            "match_result": match_result,
            "slippage_result": slippage_result,
            "commission_result": commission_result,
            "contract_multiplier": contract_multiplier,
        })

    # Backward-compatible alias for existing report/test code.
    mark_exempt = record_standalone_trade

    def try_archive(self, chain_id: str) -> None:
        record = self.chain_audit_map.get(chain_id)
        if not record: return

        # [核心修复] 风控拒单直接剥离出活跃内存
        if record.get("risk") and record["risk"].decision.value == "REJECT":
            record["archived_at"] = datetime.now()
            self.chain_audit_archive.append(self.chain_audit_map.pop(chain_id))
            return

        if not record.get("orders"): return

        all_terminal = all(ref.status in TERMINAL_STATUSES for ref in record["orders"])
        total_traded_in_orders = sum(ref.traded for ref in record["orders"])
        total_traded_in_trades = sum(t["trade"].volume for t in record["trades"])

        # 引入 1e-8 浮点容差，防止浮点误差导致终态卡死
        all_trades_recorded = math.isclose(total_traded_in_orders, total_traded_in_trades, abs_tol=1e-8)

        if all_terminal and all_trades_recorded:
            record["archived_at"] = datetime.now()

            total_slippage_cost = 0.0
            total_commission_cost = 0.0
            for t_record in record["trades"]:
                multiplier = t_record.get("contract_multiplier", 1.0)
                slip_res = t_record.get("slippage_result")
                comm_res = t_record.get("commission_result")

                if slip_res:
                    total_slippage_cost += abs(slip_res.price_diff) * t_record["trade"].volume * multiplier
                if comm_res:
                    total_commission_cost += comm_res.commission_amount

            record["summary"] = {
                "slippage_cost": total_slippage_cost,
                "commission_cost": total_commission_cost,
            }
            self.chain_audit_archive.append(self.chain_audit_map.pop(chain_id))
