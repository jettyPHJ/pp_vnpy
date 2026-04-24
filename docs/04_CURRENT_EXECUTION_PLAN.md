# 当前代码级执行清单 (Execution Checklist)

> **当前聚焦版本:** V1.3 Intent-to-Fill Traceability

## 🚫 边界控制 (Non-Goals)

* ❌ 止损单 (`StopOrder`) 与换月单 (`Rollover`) 管道接入（延至后续）。
* ❌ 复杂业务风控规则（仅跑通纯 PASS 或手动抛出 REJECT 测试链路）。
* ❌ 智能拆单逻辑（保持 1 Signal = 1 Exec）。

---

## 🛠 施工步骤

**Step 1: 建立数据结构基建 (`order_flow/models.py`)**

* 定义枚举：`OrderSource`, `RiskDecision`。
* 定义 Dataclass：`SignalOrder`, `RiskOrder`, `ExecutionOrder`。

**Step 2: 组装 ExecutionAdapter (临时安置 `execution_adapter.py`)**

* 编写 `map_order()`：迁移物理映射与摩擦处理逻辑。

**Step 3: 包装管道入口 (`CtaEngine.send_order`)**

* 入口拦截 -> 构建 `SignalOrder` -> 经过 Pipeline 拦截审查 -> 发往底层 -> 绑定 `vt_orderid` 与 `chain_id`。

**Step 4: 闭环回传 (`CtaEngine.process_trade_event`)**

* 回调拦截 -> 查字典归档 `TradeData` -> 严格按多空逻辑更新 `actual_pos` -> 终态清理落盘 -> 回调 `strategy.on_trade()`。
