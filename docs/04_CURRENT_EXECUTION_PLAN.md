# 当前代码级执行清单 (Execution Checklist)

> **当前聚焦版本:** V1.3 Intent-to-Fill Traceability (回测闭环与验证门禁)
> **修订背景:** 实盘引擎 (CtaEngine) 已跑通 V1.3 Pipeline 骨架，当前核心任务是消除 `BacktestingEngine` 的代码分叉，完成三仓位隔离与测试体系闭环。

## 🚫 边界控制 (Non-Goals)

* ❌ **暂不引入异步/数据库落盘复杂度**：终态订单清理先做**内存级隔离归档**（活跃字典 -> 历史列表），后续阶段再考虑 SQLite 或异步 JSON 落盘。
* ❌ **暂不破坏历史策略兼容性**：不强制删除策略对 `self.pos` 的读取，而是通过引擎底层的真实仓位推演后“反向赋值”来实现向下兼容。
* ❌ **暂不开发 V1.4 动态摩擦**：必须在系统验证器 (`system_validator.py`) 100% 跑绿、意图审计表可用之后，才允许开启 V1.4 任务。
* ❌ **暂不重构止损/换月单管道**：继续保持豁免，但要求在日志和统计中打上明确的 `[EXEMPT]` 标签以防审计黑洞。

---

## 🛠 施工步骤 (Milestones)

### 🎯 Step 1: 抽取公共发单流水线 (Pipeline Abstraction)

* **目标**：杜绝实盘和回测两套发单语义分叉。
* **动作**：
  * 在 `vnpy_ctastrategy/order_flow/` 下新建管理模块（如 `pipeline_manager.py` 或 `tracker.py`）。
  * 将 `CtaEngine` 中目前硬编码的 `chain_id` 生成、`SignalOrder` 构造、风控调用、执行映射以及血缘字典（`chain_audit_map`）的管理逻辑提取为公共类。

### 🎯 Step 2: 回测引擎接入流水线 (Backtest Integration)

* **目标**：让回测环境生成与实盘完全一致的审计溯源数据。
* **动作**：
  * **⚠️ 定位文件**：打开 `vnpy_ctastrategy/backtesting.py`（而非 UI 层的 wrapper）。
  * 重构 `BacktestingEngine.send_order()`，引入 Step 1 的公共 Pipeline。
  * 对 `StopOrder` 的触发路径补充 `exempt_trade_count` 统计，并在回测日志中打印 `[EXEMPT]` 标记，明确技术债边界。

### 🎯 Step 3: 落实物理仓位隔离 (Position Ledger)

* **目标**：收回策略对真实仓位的篡改权，实现单一真相来源。
* **动作**：
  * 在 `BacktestingEngine` 和 `CtaEngine` 中新增 `actual_pos_map: dict[str, int]`。
  * 改造 `process_trade_event()`：严格按 `TradeData` 的 `Direction` 与 `Offset` 对 `actual_pos_map[vt_symbol]` 进行加减积分推演。
  * **向下兼容**：推演完成后，执行 `strategy.pos = self.actual_pos_map[strategy.vt_symbol]`，让策略内的 `pos` 降级为只读镜像。

### 🎯 Step 4: 终态生命周期与防爆清理 (Terminal-State Cleanup)

* **目标**：满足 DoD 验证 5，解决长期运行的 OOM 隐患。
* **动作**：
  * 新增内存归档区：`chain_audit_archive: list`。
  * 在 `process_order_event()` 或 `process_trade_event()` 尾部增加校验：当某一 `chain_id` 下属的所有 `PhysicalOrderRef` 全部达到终态（`ALLTRADED`, `CANCELLED`, `REJECTED`）时，将其完整字典从 `chain_audit_map` 中 `pop()` 出，并 `append` 到 `chain_audit_archive` 中。

### 🎯 Step 5: 测试门禁与报告看板升级 (Validation & Reporting)

* **目标**：让自动化测试真正发挥护城河作用。
* **动作**：
  * 修复 `test/system_validator.py`：对齐最新的 `chain_audit_map` 数据结构（注意字典层级与 `PhysicalOrderRef` 的对象读取），修正 `assert not all_terminal` 等反向断言逻辑。独立推演净仓位与 `actual_pos_map` 对账。
  * **高优**：修改 `test/report_builder.py` 与相关 HTML/JS 模板，在最终的回测报告看板中新增一张 **“意图链路审计表 (Intent-to-Fill Audit Log)”**。
    * 必需列：`chain_id` | `signal_action` | `risk_decision` | `vt_orderid` | `trade_price` | `physical_cost`。
