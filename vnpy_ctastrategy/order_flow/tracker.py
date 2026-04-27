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

    def record_trade(self, trade: TradeData) -> None:
        if trade.vt_tradeid in self.exempt_tradeids:
            return

        chain_id = self.orderid_chain_map.get(trade.vt_orderid)
        if not chain_id or chain_id not in self.chain_audit_map:
            self.untracked_trade_count += 1
            return

        self.chain_audit_map[chain_id]["trades"].append(trade)
        self.try_archive(chain_id)

    def mark_exempt(self, trade: TradeData, reason: str = "STOP_ORDER"):
        self.exempt_tradeids.add(trade.vt_tradeid)
        # 统一使用 dict 存储，避免动态属性污染
        self.exempt_trade_records.append({"trade": trade, "reason": reason})

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
        total_traded_in_trades = sum(t.volume for t in record["trades"])

        # 引入 1e-8 浮点容差，防止浮点误差导致终态卡死
        all_trades_recorded = math.isclose(total_traded_in_orders, total_traded_in_trades, abs_tol=1e-8)

        if all_terminal and all_trades_recorded:
            record["archived_at"] = datetime.now()
            self.chain_audit_archive.append(self.chain_audit_map.pop(chain_id))
