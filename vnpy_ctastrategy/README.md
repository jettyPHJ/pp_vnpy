# 🧪 执行摩擦模型技术规范 (Friction Model Spec)

## 1. 架构解耦设计

执行摩擦 = 市场冲击损耗 (Slippage) + 交易通道费 (Commission)

### A. 滑点模型 (SlippageModel)

* **接口设计**: `get_execution_price(order, bar, tick_size) -> SlippageResult`
* **返回对象规范 (`SlippageResult`)**:
  * `execution_price`: 最终执行价
  * `price_diff`: 价格偏移量 *(带符号: `execution_price - signal_price`)*
  * `model_name`: 模型名称
  * `reason`: 审计原因快照 (如 "触发最大阈值截断")
* **V1.4 核心算法 (受控非线性冲击模型 VolumeImpactModel)**:
  为防止除以 0 及极端黑洞滑点，必须采用以下保护公式：
  1. `volume_ratio = min(OrderVolume / max(BarVolume, 1), MaxVolumeRatio)`
  2. `DynamicSlippageTicks = BaseTicks * (1 + ImpactFactor * (volume_ratio ^ Alpha))`
  3. `FinalSlippageTicks = min(DynamicSlippageTicks, MaxSlippageTicks)`
  4. 最终价格依据买卖方向施加对应 Ticks 的惩罚。

### B. 费率模型 (CommissionModel)

* **接口设计**: `calculate_commission(symbol, execution_price, volume, contract_multiplier) -> CommissionResult`
* **说明**：`contract_multiplier` 由调用方（引擎）通过查表获取并无状态传入。

## 2. 引擎配置标准 (Configuration)

回测系统启动时，通过 `friction_setting` 字典进行标准化注入：

```python
friction_setting = {
    "slippage": {
        "model": "volume_impact",
        "params": {"base_ticks": 1, "alpha": 1.5, "impact_factor": 1.0, "max_volume_ratio": 0.1, "max_slippage_ticks": 20}
    },
    "commission": {
        "model": "tier_based", 
        "params": {"default_rate": 0.0001}
    }
}
```
