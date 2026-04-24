# 📈 系统演进路线图 (Evolution Roadmap)

## Phase 1: 夯实投研底座（✅ 基本完成）

实现连续合约映射、换月对账机制，跑通单品种回测。
*(遗留项：参数优化的稳定性验证随 V1.3 同步推进。)*

## Phase 2: V1.3 统一发单管道与意图审计（📍 当前阶段）

**目标**：解决“执行一致性”，在 `vnpy_ctastrategy` 内跑通全链路流水线骨架。

* 引入 TraceID (`chain_id`) 模型，实现 Signal -> Risk -> Execution 审计链。
* 建立 `ExecutionAdapter` 雏形（暂存 `order_flow` 目录下，临时调用基础数据）。

## Phase 3: 实盘前置与安全分离（🚀 下一阶段）

由于实盘接入工作量庞大，分为 A/B 两步走：

### Phase 3A: 实盘路由分离 (Execution First)

* **剥离 Execution**：将 V1.3 固化接口的 `ExecutionAdapter` 抽离为独立的 `vnpy_execution`，确立其作为唯一执行入口的地位。
* 接入 QMT / CTP 模拟盘通道，跑通底层 Order/Trade 回报闭环。

### Phase 3B: 风控与对账独立 (Security & Ops)

* **新建 Ops 对账**：引入独立 `ReconcileService`，支持 Broker 仓位 Snapshot 启动校准。
* **剥离 Risk**：将 `risk_layer` 抽离为独立的 `vnpy_riskmanager`，正式接入单日流控与回撤熔断。

## Phase 4: 本地数据资产与投研闭环

**目标**：沉淀机构级独有数据资产，升级 `vnpy_data_asset`。

## Phase 5: 截面与组合扩展（🔮 远期规划）

**目标**：多品种/截面协同的投资组合系统。
