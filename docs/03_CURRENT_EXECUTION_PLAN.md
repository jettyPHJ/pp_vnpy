# V1.4 执行摩擦模块化 实施蓝图

> **核心定调** ：
> 彻底分离“价格域（撮合改价）”与“金额域（财务归因）”。滑点直接体现在真实成交价（`TradeData.price`）中；引入 `ExecutionMatchResult` 将撮合意图与行为判定下沉；通过默认开启的 `legacy` 标志位和 Shim 机制实现新旧逻辑的绝对安全隔离。

---

### 📦 施工包 1：数据结构与领域模型 (Domain Models)

 **位置** : `vnpy_ctastrategy/order_flow/friction.py` (新建)

**1. 撮合行为状态机 (`MatchBehavior`)**

* `PASSIVE_LIMIT`：限价单被动成交（零滑点）。
* `AGGRESSIVE_LIMIT`：限价单主动穿价（承担滑点）。
* `STOP_TRIGGERED`：止损单触发（市价滑点）。
* `MARKET_ORDER`：市价单。

**2. 核心数据载体 (Dataclasses)**

* **`ExecutionMatchResult`** (由执行/撮合层统一返回)：
  * `matched: bool`
  * `signal_price: float` (意图价：MARKET取开盘/当前，LIMIT取order.price，STOP取触发价)
  * `match_price: float` (无滑点的理论撮合价)
  * `volume: float`
  * `behavior: MatchBehavior`
  * `reason: str`
* **`SlippageResult`** (由滑点模型返回)：
  * `execution_price: float` (加滑点后的真实成交价)
  * `price_diff: float` (带符号偏移量：执行价 - 撮合价)
  * `model_name: str`
* **`CommissionResult`** (由手续费模型返回)：
  * `commission_amount: float` (绝对金额)
  * `model_name: str`

**3. 模型接口**

* `BaseSlippageModel.calculate(order: OrderData, match_result: ExecutionMatchResult, contract_multiplier: float) -> SlippageResult`
* `BaseCommissionModel.calculate(trade: TradeData, contract_multiplier: float) -> CommissionResult`

---

### 📦 施工包 2：执行引擎与撮合重构 (Engine & Matching)

 **位置** : `vnpy_ctastrategy/backtesting.py`

**1. 双轨切换开关 (`friction_mode`)**

* `BacktestingEngine` 初始化或 `set_parameters` 引入 `friction_mode` 标志位， **默认值为 `"legacy"`** （显式传入 `"v1.4"` 才开启新路径，保障历史脚本零侵入）。

**2. 撮合逻辑下沉与流水线**
改造 `cross_limit_order` / `cross_stop_order` 内部链路：

1. 引擎调用执行层判定逻辑，获取完整的 `ExecutionMatchResult`。
2. 引擎调用 `SlippageModel.calculate(...)` 获取 `SlippageResult`。
3. **关键改价** ：生成 `TradeData`，其 `price` 强制赋值为 `SlippageResult.execution_price`。
4. 调用 `CommissionModel.calculate(...)` 算手续费。
5. 将 `TradeData`, `ExecutionMatchResult`, `SlippageResult`, `CommissionResult`, `contract_multiplier` 打包传给 `Tracker` 归档。

---

### 📦 施工包 3：PnL 对齐与换月逻辑隔离 (PnL & Rollover Shim)

 **位置** : `vnpy_ctastrategy/backtesting.py`

**1. 换月路径隔离 (Rollover Shim)**
换月逻辑保持原样，其产生的模拟订单继续调用旧的 `get_slippage(trade, size)` 和 `get_commission()` 接口，绝不走 V1.4 新链路，直至 V1.5 统一处理。

**2. 杜绝 DailyResult 双重扣费**
`DailyResult.calculate_pnl` 根据 `friction_mode` 分流：

* `legacy`: 维持原逻辑 `net_pnl = total_pnl - commission - slippage + rollover_pnl`。
* `v1.4`: 滑点已含在 `trade.price` 中，改为  **`net_pnl = total_pnl - commission + rollover_pnl`** （彻底摘除 `- slippage`，手续费继续按现金成本扣减）。

---

### 📦 施工包 4：审计归档与字段语义明确 (Tracker & Semantics)

 **位置** : `vnpy_ctastrategy/order_flow/tracker.py`

**1. 字段语义唯一真相表**

* `signal_price`：策略发单的逻辑意图基准价（来自 `ExecutionMatchResult`）。
* `match_price`：不考虑流动性的理论成交价（来自 `ExecutionMatchResult`）。
* `execution_price`：考虑摩擦后的真实物理成交价（等于 `TradeData.price`，来自 `SlippageResult`）。

**2. Tracker 财务核算**
`record_trade` 接收新对象组。在 `try_archive` 终态时计算：

* `slippage_cost = abs(price_diff) * volume * contract_multiplier`
* `commission_cost = commission_amount`
* 写入 `chain_audit_archive` 字典供报告渲染。

**3. 止损单临时审计**
在 `mark_exempt` 豁免止损单时，必须同步传入并记录摩擦结果（注：V1.5 再将 StopOrder 正式纳入 Pipeline）。

---

### 📦 施工包 5：验收测试设计 (System Validation)

 **位置** : `test/system_validator.py`

合并前必须新增并跑通以下 4 个核心测试：

1. **滑点方向约束测试 (Directional Constraint)** ：

* 买单 (BUY): 断言 `execution_price >= match_price`。
* 卖单 (SELL/SHORT): 断言 `execution_price <= match_price`。
* 被动限价 (PASSIVE_LIMIT): 断言 `execution_price == match_price`。

1. **防双重扣费测试 (Double-Deduction Check)** ：

* 构造单笔开平、固定收盘价、无手续费的极简场景， **测试配置中显式关闭换月功能** 。
* 断言开启滑点前后 `net_pnl` 的差额，严格等于 `Tracker` 中累加的 `sum(slippage_cost)`。

1. **意图价严格性检查 (Signal Price Check)** ：

* 抽查归档记录，断言 `signal_price` 绝不允许为 `0` 或 `None`。

1. **行为判定精准度 (`V14MockStrategy` 场景测试)** ：

* 设定价格诱发穿价 ➡️ 断言生成 `AGGRESSIVE_LIMIT`。
* 设定价格区间内 ➡️ 断言生成 `PASSIVE_LIMIT`。
* 设定击穿止损 ➡️ 断言生成 `STOP_TRIGGERED`。
