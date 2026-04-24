# 🧪 系统验证与验收标准 (System Validation & DoD)

> **核心原则**：文档定义规则，代码执行验证。不满足当前 DoD 的代码绝对不允许合入主线。

## 🎯 V1.3 & V1.4: Intent-to-Fill & Friction Isolation

系统必须通过以下 6 项自动化/半自动化验证，否则视作未完成。

### ✅ 验证 1：仓位唯一真相约束 (No Shortcut Constraint)

* **测试方法**：运行结束后，调用验证脚本对比引擎仓位与流水积分。
* **通过标准**：
  * 引擎最终的 `actual_pos`，必须与按合约、开平标识（Direction & Offset）严密推演后的净仓位完全一致。
  * **计算范式**：必须分离统计 `long_pos` 与 `short_pos`，最后推导 `net_pos = long_pos - short_pos`。
  * 发现策略绕过管道篡改 `actual_pos` 时，系统在 Validation 阶段必须阻断并抛出 `FATAL`。

### ✅ 验证 2：意图全链路溯源 (Full Pipeline Traceability)

* **测试方法**：随机抽取底层回传的任一 `TradeData`。
* **通过标准**：必须能通过 `vt_orderid` 定位到唯一的 `exec_id`，再向上溯源到原始的 `chain_id` 和 `SignalOrder`。不允许存在无头成交。

### ✅ 验证 3：风控熔断拦截有效性 (Risk Intercept)

* **测试方法**：开启硬阻断规则触发发单。
* **通过标准**：生成 `REJECTED` 状态日志，订单未发往底层，`actual_pos` 保持不变。

### ✅ 验证 4：历史策略向下兼容 (Zero-Intrusion)

* **测试方法**：不做代码修改，直接运行 V1.0 时代的基线策略。
* **通过标准**：正常发单，回测无报错，且溯源日志生成完备。

### ✅ 验证 5：基于终态转移的审计防爆机制 (Terminal-State Cleanup) 🚨

* **测试方法**：注入包含部分成交、主动撤单、风控拒单的复杂交易流。
* **通过标准**：
  * 系统必须实现**基于状态机的生命周期管理**。
  * 当且仅当一个 `chain_id` 下属的所有订单达到绝对终态（`ALLTRADED`, `CANCELLED`, `REJECTED`），该日志必须被安全落盘归档，并从活跃的 `chain_audit_map` 内存字典中剥离，防止实盘 OOM。

### ✅ 验证 6：执行摩擦模块化 (Friction Isolation)

* **测试方法**：在不修改策略代码的情况下，动态切换 `Execution Layer` 的滑点模型。
* **通过标准**：系统 PnL 能够立即反映出不同的摩擦结果，且审计日志中记录了每笔成交承担的“摩擦成本归因”。
