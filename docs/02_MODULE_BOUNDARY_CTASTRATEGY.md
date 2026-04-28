# 🚧 CTA 策略与引擎模块边界定义 (Module Boundaries)

> **Notice:** 明确界定 `Strategy`、`Engine` 与 `Ops Pipeline` 之间的操作权限与数据流向，杜绝隐式耦合。

## 1. 策略层边界 (Strategy)

* **可读权**：仅能读取行情数据及只读的业务状态参数。
* **写入权**：**严格禁止**篡改底层物理持仓字典。唯一输出是发出 `SignalOrder` 表达逻辑意图。

## 2. 引擎层边界 (Engine as Orchestrator)

回测引擎 (`BacktestingEngine`) 与实盘引擎 (`CtaEngine`) 必须保持“枢纽 (Hub)”的纯洁性：

* **禁止隐式计算**：引擎捕获撮合事件后，只负责调用模型获取 `SlippageResult`。严禁引擎直接执行乘法计算总摩擦金额。
* **静态数据无状态透传**：引擎从底层的合约数据对象（`ContractData`）中提取 `contract_multiplier`，并将其作为参数透传给执行层（算费率）与对账层（算金额），确保 Tracker 和 Model 的完全无状态性。
* **动态上下文透传 (Dynamic Context Pass-through)**：针对 V1.5 的上下文感知滑点模型，引擎必须负责从行情流中提取 `MarketContext`（如前 N 根已完成 Bar 的均量、ATR），并在调用模型时向下透传。
* **无状态约束**：执行层模型严禁持有行情引用或向上反调引擎获取数据，必须保持绝对的无状态计算。

## 3. 流水线与对账层边界 (Ops Pipeline & Tracker)

* **成本核算 (Accounting)**：接收来自引擎的 `SlippageResult`、`CommissionResult` 以及引擎透传的 `contract_multiplier`，负责将其计算出最终的财务成本（金额域）。
* **唯一真相 (Single Source of Truth)**：维持 `chain_audit_map` 作为所有逻辑到物理链路状态的最终归档地。
