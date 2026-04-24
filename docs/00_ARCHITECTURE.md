# 🏛️ 极简量化交易系统全局架构 (System Architecture)

> **Status:** BASELINE CANDIDATE / v1 baseline
> **Last Updated:** 2026-04-24
> **Notice:** 本文档定义了系统的最高宪法与不变量。任何对此架构边界的修改，必须经过显式的架构评审 (Revision)。

## 🚨 系统长期不变量 (System Invariants)

为了防止系统在长期迭代中腐化，系统设定以下绝对不可违反的红线：

1. **单一发单管道 (Single Pipeline Constraint)**：所有订单（策略意图、止损、换月、手工、强平）**必须**通过统一 Pipeline 进入执行层。任何试图绕过 Pipeline 直接调用底层发单接口的行为，视为严重架构违规。*(注：V1.3 MVP 阶段仅覆盖常规策略发单，Stop/Rollover 将在 Iteration 2/3 强制接入)*。
2. **仓位唯一真相来源 (Single Source of Truth for Position)**：
   * 运行期：`actual_pos` 仅由底层撮合回调（`TradeData`）增量更新。
   * 恢复期（启动/断线）：允许 Broker Position Snapshot 发起全量校准，但必须显式标记 `pos_source` 并记录校准审计事件。
3. **职责单向流动 (Unidirectional Dependency)**：策略层只管“意图”，风控层只管“审批”，执行层只管“翻译与发送”。严禁越权代办。

---

## 🏗️ 6 大核心逻辑层定义

### 1. 投研与仿真层 (Research Layer)

* **核心模块**：`vnpy_ctabacktester`
* **职责**：历史数据回放仿真、参数优化、统计报告。

### 2. 策略与信号层 (Strategy Layer)

* **核心模块**：`vnpy_ctastrategy`
* **职责**：承载 Alpha 逻辑，生成纯逻辑交易意图（`SignalOrder`）并赋予追溯码 `chain_id`。
* **边界约束**：绝对不处理物理合约映射；绝对不直接调用底层发单 API；**只接收从 Ops/Execution 层归一化处理后的“逻辑成交事件”，不负责解析物理成交明细**。

### 3. 风控层 (Risk Layer)

* **核心模块**：`vnpy_riskmanager` (V1.3 暂内嵌，Phase 3 独立)
* **明确职责（只做这三件事）**：
  - 是否允许发单 (allow / reject)
  - 允许发多少 (计算并更新额度 `allowed_pos`)
  - 当前风险状态判定 (HARD / RATE / BREAKER)
* **边界约束**：**不负责**合约选择、不负责价格换算、不负责执行路由分配、不负责底层成交处理。

### 4. 执行路由层 (Execution Layer)

* **核心模块**：`vnpy_execution` (计划独立)
* **生命周期规划**：
  - **V1.3**：暂存于 `vnpy_ctastrategy/order_flow` 目录下（过渡状态）。
  - **Phase 3**：正式抽象为独立的 `vnpy_execution` 模块。
  - **长期**：成为系统对接外界（QMT / CTP / 模拟器）的**唯一物理执行入口**。
* **职责**：逻辑合约转物理合约（`ExecutionAdapter`）、Price Offset 处理、Tick Size 对齐。

### 5. 状态与对账层 (Ops & Reconciliation Layer)

* **核心模块**：`PositionState` / `ReconcileService`
* **职责**：
  - **持有并维护三仓位模型**：`target_pos` (**策略专属写入**), `allowed_pos` (**引擎/风控写入**，策略只读), `actual_pos` (**成交/对账写入**)。
  - **审计枢纽**：存储与归档 `chain_id` 交易审计日志。
  - **实盘防线**：Broker 账单核对、断线脏数据清洗、幽灵仓位阻断。

### 6. 数据资产层 (Data Layer)

* **核心模块**：`vnpy_data_asset`
* **职责**：提供连续合约映射规则、换月日历、行情与因子特征库。
* **边界约束**：只提供“客观市场数据”，不存储“主观交易与审计日志”（审计日志归 Ops 层）。
