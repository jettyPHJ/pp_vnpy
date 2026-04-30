# 🚀 V1.6 Tick 模拟回放 实施蓝图

> **[Archive] V1.5 归档声明** ：V1.5 施工包已全部验收通过。`CapitalAndSizeRiskManager` 动态容量风控与 `VolumeImpactSlippageModel` 冲击模型已成功实装，双账本对账（含 Rollover 换月成本）通过财务核验。现作为稳定底座归档，V1.6 在此之上构建。

---

## 🎯 V1.6 核心导向

**V1.6 的目标绝不是构建微秒级高频仿真器，而是在中低频 CTA 范围内，将 Bar 级产生的逻辑订单投入物理合约 Tick 路径中，验证其成交可行性，量化识别 Bar 模式下不可见的执行损耗。**

> **V1.6 核心不变量** ：Bar 信号生成逻辑保持不变；Tick 数据只允许在订单生成之后参与成交判断，不参与信号形成。

---

## 📦 施工包 1：物理 Tick 数据加载与时间语义对齐

 **核心任务** ：保持 `ContinuousBuilder` 专职负责连续合约 Bar 的构建逻辑，新增 `TickReplayStore` 负责按时间轴顺序读取物理合约的 Tick 数据流，两者职责严格隔离。

 **时间语义红线约束** ：

* 严格落实 Bar 信号与 Tick 执行的时间隔离。一根在 `T` 时刻收盘的 Bar 所生成的订单，必须且只能流入 **`> T` 的有效可交易 Tick 序列**中撮合，严禁本 Bar 信号消费本 Bar 内的 Tick。
* 换月日特殊处理：时间对齐规则对换月日同样适用。`_do_rollover` 触发后生成的换月订单，只能流入换月后**新物理合约**的 Tick 序列，严禁使用旧合约 Tick 或换月 Bar 本身的 Tick 进行撮合。

---

## 📦 施工包 2：执行上下文与 TickExecutionModel 骨架

 **定义 `TickExecutionContext`** ：建立 Tick 级执行上下文，包含以下核心字段：

* `bid1, ask1`：当前 Tick 最优买卖价。
* `bid_vol_1, ask_vol_1`：当前 Tick 最优档位可见量。
* `last_price`：最新成交价。
* `delta_volume`：本 Tick 相对上一 Tick 的成交量增量。
* `spread`：当前买卖价差。
* `mid_price`：中间价，用于执行质量偏离度计算。

 **模型骨架任务** ：开发 `TickExecutionModel`，替换原有 `last_price` 等效假设；输出扩展版 `ExecutionMatchResult`，新增 `partial_fill_volume`（本 Tick 实际成交量）与 `remaining_volume`（剩余未成交量）字段。

---

## 📦 施工包 3：主动/被动成交规则与 Fill Modes

针对 L1 盘口无法看穿真实队列的限制，建立工程级合理近似。以下模式通过配置参数选择，互不默认覆盖：

 **主动吃单模式 (Active Fill)** ，适用于 `AGGRESSIVE_LIMIT` 与 `STOP_TRIGGERED`：

* `FULL_ON_TOUCH`：触价全额成交（乐观基线，用于与 Bar 模式对比）。
* `TOP_OF_BOOK_CAPPED`：成交量严格受限于当前 Tick 盘口一档可见量（`ask_vol_1` / `bid_vol_1`），超出部分转为剩余排队量。
* `PARTICIPATION_CAPPED`：基于区间 `delta_volume` 参与率上限裁剪（压力测试模式）。

 **被动挂单模式 (Passive Fill Policy)** ，适用于 `PASSIVE_LIMIT`：

* `TOUCH`：`last_price` 触及挂单价即允许成交（较乐观）。
* `TRADE_THROUGH`：`last_price` 必须完全穿透挂单价才允许成交（偏保守）。
* `VOLUME_PROB`：按 Tick 增量成交量结合挂单位置进行概率成交估算（实验性）。

---

## 📦 施工包 4：部分成交、剩余订单与订单有效期 (TIF)

 **生命周期闭环** ：引擎侧新增 Active Order Book 子模块，接管由 `TickExecutionModel` 弹出的碎片化剩余量，确保剩余订单在后续 Tick 中被持续处理直至闭环，绝不允许静默丢失。

 **订单有效期（TIF）最小实现** ：

* `BAR_END`：当前信号 Bar 所对应的 Tick 周期结束后，自动撤销所有剩余未成交量，生成撤单审计记录。
* `GTC (Good Till Cancelled)`：持续有效，跨 Bar 挂单，直到完全成交或策略主动撤单。

---

## 📦 施工包 5：审计链路升级与 Bar-vs-Tick 对比报告

报表须证明差异，而非只展示结果。新增独立的执行质量对比区域：

* 主动吃单比例 vs 被动挂单成交比例。
* Bar 模式全量成交但 Tick 模式部分成交/未成交的订单数量分布与原因归因。
* 平均成交价相对 mid-price 的偏移（买入偏高 / 卖出偏低）。
* 同一策略在 `FULL_ON_TOUCH` 基线与 `TOP_OF_BOOK_CAPPED` 模式下的净收益对比，量化执行损耗区间。

---

## 📦 施工包 6：验收测试与回归保护 (Acceptance Tests)

1. **防穿越测试** ：Bar 收盘信号所生成的订单，严禁以当前 Bar 内部或更早时间截点的 Tick 成交。换月日场景同样纳入此测试。
2. **量级裁断测试** ：当订单量 > 当前 Tick 盘口一档可见量时，`TOP_OF_BOOK_CAPPED` 模式必须返回 `Status.PARTTRADED`，剩余量必须进入 Active Order Book 持续排队。
3. **状态连续性测试** ：部分成交的剩余订单在 TIF 到期或完全成交前，必须在后续每个相关 Tick 中均被正确处理，`chain_audit_map` 须完整记录每次状态流转，绝不允许中间状态静默丢失。
4. **差异可解释测试** ：同一策略在 Bar 模式与 Tick Replay 模式下的结果，必须产生 **可解释的合理差异** ——被动单成交率下降、滑点分布向不利方向偏移、部分成交导致仓位建立延迟。审计报告须精准定位差异来源，差异不可解释即视为测试不通过。
