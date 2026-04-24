# vnpy_ctastrategy: 核心策略引擎内部设计

本引擎是交易意图的发源地，从 V1.3 开始，重构为**基于统一流水线 (Pipeline) 的事件驱动状态机**。

## ⚠️ 策略开发者红线警告 (CRITICAL)

为了保证底层状态机（Ops 层）的绝对纯洁性，策略开发者必须遵守以下铁律：

1. **禁止越权修改**：绝对禁止直接访问/修改 `orderid_chain_map` 或 `chain_audit_map`。
2. **禁止篡改真相**：绝对禁止在策略内部通过局部计算去修改 `self.actual_pos`。
3. **只管提出意图**：策略只能写入 `target_pos`（我要多少）。现实如何成交，由引擎全权处理并通过归一化的 `on_trade` 通知策略。

## 🧠 核心设计哲学

### 1. 投研 API 保持不变

策略调用 `buy()`, `sell()` 的体验完全不变。引擎会在**底层入口处**将其拦截，并封装为带有全局生命周期追踪码 (`chain_id`) 的 `SignalOrder`。

### 2. TraceID / SpanID 溯源模型

* **`chain_id` (TraceID)**：一笔交易意图的全局唯一标识。
* **`exec_id` (SpanID)**：代表一次“物理动作”。拆单场景下，一个 `chain_id` 会衍生多个 `exec_id`。

### 3. 三仓位状态模型 (严格隔离)

* `target_pos`：策略意图，策略专属写入。
* `allowed_pos`：风控额度，引擎维护，策略只读（或通过降级回调接收）。
* `actual_pos`：物理真实，仅由 `TradeData` 回调与对账服务 `Snapshot` 更新。
