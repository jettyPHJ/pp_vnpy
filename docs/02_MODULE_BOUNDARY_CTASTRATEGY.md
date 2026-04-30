# 🚧 CTA 策略与引擎模块边界定义 (Module Boundaries)

> **Notice:** 明确界定 `Strategy`、`Engine` 与 `Ops Pipeline` 之间的操作权限与数据流向，杜绝隐式耦合。V1.6 起新增 Tick 执行维度，特别强化执行路由层与引擎层的边界约束，防止 Tick 数据污染信号链路。

## 1. 策略层边界 (Strategy Layer)

* **可读权** ：仅能读取行情数据及只读的业务状态参数。V1.6 默认服务于中低频 CTA，策略信号入口仍以 `on_bar` 为主；`on_tick` 不作为默认 Alpha 信号入口，除非显式声明为 Tick 策略模式。
* **写入权** ：**严格禁止**篡改底层物理持仓字典。唯一输出是发出 `SignalOrder` 表达逻辑意图。
* **防未来函数边界** ：禁止策略层直接读取 `TickExecutionModel` 的成交回放细节（如盘口价、部分成交量）来反向影响信号生成，防止引入未来函数。

## 2. 引擎层边界 (Engine as Orchestrator)

回测引擎 (`BacktestingEngine`) 与实盘引擎 (`CtaEngine`) 必须保持"枢纽 (Hub)"的纯洁性：

* **禁止隐式计算** ：引擎捕获撮合事件后，只负责调用模型获取 `SlippageResult`。严禁引擎直接执行乘法计算总摩擦金额。
* **静态数据无状态透传** ：引擎从底层的合约数据对象（`ContractData`）中提取 `contract_multiplier`，并将其作为参数透传给执行层（算费率）与对账层（算金额），确保 Tracker 和 Model 的完全无状态性。
* **动态上下文透传 (Dynamic Context Pass-through)** ：引擎必须负责从行情流中提取 `MarketContext`（Bar 级，含均量、ATR）与 `TickExecutionContext`（Tick 级，含 bid/ask 盘口与增量成交量），并按订单时间语义透传给对应的 ExecutionModel。
* **滑动窗口维护权归属** ：`MarketContext.reference_volume` 在 Bar 模式下的滑动均量窗口，以及 Tick 模式下的增量成交量窗口， **维护更新权绝对归属引擎层** （分别在 `new_bar` / `new_tick` 中维护）。执行模型只允许被动消费，严禁主动回查引擎状态。
* **非干涉原则** ：引擎不得直接根据 bid/ask 生成成交价，成交判定必须完全交给 ExecutionModel。

## 3. 执行路由层边界 (Execution Routing Layer)

本层包含原有的 `BarExecutionModel` 与 V1.6 新增的 `TickExecutionModel`，两者均须保持 **绝对无状态** 。

 **`BarExecutionModel` 行为定义** ：价格穿透即触发，不感知盘口深度，返回全量成交结果。

 **`TickExecutionModel` 边界定义（V1.6 新增）** ：

* **必须做** ：
* 基于 `bid1/ask1` 判定成交触发条件（主动吃单）及 `ask1/bid1` 判定被动挂单成交条件。
* 消耗 `bid_vol_1/ask_vol_1` 进行部分成交量计算，返回含剩余未成交量的扩展版 `ExecutionMatchResult`。
* 返回明确的成交行为标记（`AGGRESSIVE / PASSIVE / PARTIAL`），供对账层归档执行质量。
* **严禁做** ：
* 自行管理订单的完整生命周期（不能直接删除未成交订单，生命周期管理归属引擎侧）。
* 对资金、保证金、容量风控进行二次检查（风控只在 Pipeline 层执行一次）。
* 计算复杂的佣金与滑点账本金额（成本核算交由 Friction Layer）。
* 读取任何策略状态或持仓信息。

 **引擎侧 Active Order Book（V1.6 新增内部子模块）** ：

> 说明：此处为引擎层内部新增的子模块边界，并非新增独立架构层，不改变六层架构定义。

* **职责** ：维护内存中的未成交订单；管理部分成交（`PARTTRADED`）后的剩余排队量；处理订单有效期（Time-In-Force，如 `BAR_END`、`GTC`）引发的超时与自动撤单。
* **禁区** ：绝对不自行生成撮合价格；所有订单状态流转必须由 `ExecutionMatchResult` 驱动，不绕过 Tracker 的审计记录。

## 4. 流水线与对账层边界 (Ops Pipeline & Tracker)

* **成本核算 (Accounting)** ：接收来自引擎的 `SlippageResult`、`CommissionResult` 以及引擎透传的 `contract_multiplier`，负责将其计算出最终的财务成本（金额域）。
* **唯一真相 (Single Source of Truth)** ：维持 `chain_audit_map` 作为所有逻辑到物理链路状态的最终归档地。
* **执行质量扩充** ：在 Tick 模式下，Ops 层须额外归档每笔成交的 Tick 执行结果，包括：成交模式（主动/被动/部分）、部分成交次数、未成交原因归因、成交价相对 mid-price 的偏移量。这些字段是证明 Tick 回放有效性的核心证据链。
