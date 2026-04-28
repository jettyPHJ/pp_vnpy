# 🚀 V1.5 容量与可执行性建模 实施蓝图

> **V1.5 核心不变量**：
> 回测系统不再假设无限流动性，任何成交必须同时满足：
>
> 1. 风控层硬约束（资金 / 规模）
> 2. 执行层软约束（滑点成本）

### 📦 施工包 1：滑点模型接口升级与实体化

**1. 接口正式化与历史债务处置**

* **遗留处理**：`get_slippage` 接口保留至换月 Shim 路径完成改造前（详见后续 V1.x 规划），届时一并退役。
* **接口重命名**：废弃 `calculate_v14` 临时命名，统一升级为正式接口：
  `def calculate(self, order, match_result, contract_multiplier, context: MarketContext) -> SlippageResult`
* **滑点作用点约定**：所有 SlippageModel 必须基于撮合引擎输出的 `match_price` 进行价格偏移，**严禁**直接修改订单原始报价（`order.price`），确保 V1.4 的价格域解耦架构不被破坏。

**2. MarketContext 定义**

* `current_atr`: 当前波动率参考。
* `reference_volume`: 参考成交量。
  * *时序约束*：必须来自前 N 根已完成 Bar 的均量或中位数，严禁使用当根未走完的 Bar。
  * *语义声明*：该字段仅用于容量与冲击成本估计，不代表当根 Bar 的真实可成交量。
  * *(注：移除了静态的 `pricetick` 字段，该属性属于物理合约元数据，由调用方直接从 `ContractData` 获取透传或在策略层处理)*

**3. 模型库扩展**

* **Level 1**: `FixedTickSlippageModel` (继承 V1.4 逻辑，用于基础验证)。
* **Level 2**:
  * `VolumeImpactSlippageModel`: 冲击成本与 `sqrt(order_vol / reference_volume)` 正相关。
  * `VolatilitySlippageModel`: 滑点锚定 `current_atr` 的特定百分比。

### 📦 施工包 2：基础执行约束 (Execution Constraints)

**1. 资金约束统合 (Capital Constraint)**

* 正式激活并管道化现有的 `BaseMarginModel`。
* 淘汰默认返回 `True` 的 `V1DefaultMarginModel`，由真正的资金拦截器统一承接校验，基于可用资金核算 `order_vol * price * multiplier * margin_rate`，超限则拒单（reject）。

**2. 规模约束 (Size Constraint)**

* `max_order_size`: 限制单笔最大报单绝对值。
* `max_participation_rate`: 订单量不得超过 `reference_volume` 的设定比例（如 10%）。
* **shrink 行为定义**：当触发上述规模约束时，订单数量按约束上限进行裁剪（shrink，即修改订单的 `volume` 属性），裁剪后的合规订单继续进入后续的撮合流程，而非完全拒单。

### 📦 施工包 3：验收测试与验证标准

1. **资金拦截测试**：设置小额初始资金运行大手数策略，断言准确触发资金约束拒单，且核心账本 `actual_pos` 严格保持不变。
2. **容量成本非递减测试**：
   * 使用 `VolumeImpactSlippageModel`，在 10 万、100 万、1000 万本金下运行同一策略。
   * 断言交易总成本（滑点金额）随资金规模呈**非递减趋势**。
   * 断言当实际成交手数增加时，滑点成本同步上升，导致该策略的净收益率发生符合逻辑的合理衰减。
