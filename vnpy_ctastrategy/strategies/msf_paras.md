#### 1. 基础运行参数

| 参数名称                    | 类型 | 默认值 | 建议范围 | 步长 | 说明与作用                                             |
| --------------------------- | ---- | ------ | -------- | ---- | ------------------------------------------------------ |
| bar_window                  | int  | 1      | 1~5      | 1    | K线周期（分钟）。1表示1分钟K线，建议商品期货用1或5分钟 |
| fixed_size                  | int  | 1      | 1~10     | 1    | 单笔基础手数（后续会被 ensemble_signal 缩放）          |
| max_pos                     | int  | 2      | 2~20     | 1    | 单边最大持仓手数（总风险控制核心参数）                 |
| min_rebalance_interval_bars | int  | 3      | 2~20     | 1    | 最小调仓间隔（防止过于频繁交易，降低手续费和滑点）     |

#### 2. 状态机核心参数（StateClassifier）

| 参数名称                   | 类型  | 默认值 | 建议范围  | 步长 | 说明与作用                                           |
| -------------------------- | ----- | ------ | --------- | ---- | ---------------------------------------------------- |
| regime_window              | int   | 20     | 15~40     | 5    | 状态评估窗口长度（用于计算 efficiency、vol_rank 等） |
| breakout_lookback          | int   | 5      | 3~10      | 1    | 突破判断的回顾窗口                                   |
| breakout_confirm_bars      | int   | 2      | 1~4       | 1    | 突破后确认延续的K线数量                              |
| vol_rank_lookback          | int   | 120    | 60~200    | 20   | 波动率分位计算的历史长度（判断是否处于高波动混沌）   |
| regime_adx_window          | int   | 14     | 10~20     | 2    | ADX 计算周期                                         |
| trend_efficiency_threshold | float | 0.42   | 0.35~0.55 | 0.02 | 趋势效率阈值（越高越严格）                           |
| chaos_efficiency_ceiling   | float | 0.30   | 0.25~0.38 | 0.02 | 混沌效率上限（低于此值 + 高波动 = 混沌）             |
| high_vol_rank_threshold    | float | 0.72   | 0.65~0.80 | 0.02 | 高波动率分位阈值（判断混沌的重要指标）               |
| regime_trend_adx_threshold | float | 22.0   | 18~28     | 1.0  | 状态机中趋势状态要求的 ADX 下限                      |
| regime_range_adx_ceiling   | float | 18.0   | 15~22     | 1.0  | 状态机中震荡状态要求的 ADX 上限                      |

#### 3. 状态平滑与切换参数

| 参数名称            | 类型 | 默认值 | 建议范围 | 步长 | 说明与作用                                |
| ------------------- | ---- | ------ | -------- | ---- | ----------------------------------------- |
| state_confirm_bars  | int  | 3      | 2~6      | 1    | 新状态需要连续确认的K线数量（防频繁切换） |
| state_cooldown_bars | int  | 8      | 5~15     | 1    | 状态切换后强制冷却周期（减少无效调仓）    |

#### 4. 权重映射参数（RoleEngine）

| 参数名称             | 类型  | 默认值 | 建议范围  | 步长 | 说明与作用                               |
| -------------------- | ----- | ------ | --------- | ---- | ---------------------------------------- |
| trend_trend_weight   | float | 0.85   | 0.70~0.95 | 0.05 | 趋势状态下趋势角色的权重                 |
| chaos_defense_weight | float | 0.80   | 0.70~1.0  | 0.05 | 混沌状态下防守角色的权重（核心防守参数） |

（其余权重参数类似，保持趋势状态偏趋势、震荡状态偏震荡、混沌状态偏防守的原则）

#### 5. 风险与执行参数

| 参数名称                 | 类型  | 默认值 | 建议范围  | 步长 | 说明与作用                                         |
| ------------------------ | ----- | ------ | --------- | ---- | -------------------------------------------------- |
| chaos_max_position_scale | float | 0.50   | 0.20~0.60 | 0.05 | 混沌状态允许的最大仓位比例（0.0=纯空仓，0.5=半仓） |
| entry_signal_threshold   | float | 0.70   | 0.55~0.85 | 0.05 | 开仓信号强度阈值（越高越保守）                     |
| exit_signal_threshold    | float | 0.25   | 0.15~0.40 | 0.05 | 平仓信号强度阈值（越低越容易平仓）                 |
| rebalance_tolerance      | float | 1.0    | 1~3       | 0.5  | 仓位偏差容忍度（大于此值才调仓）                   |

#### 6. 趋势模块参数（SimpleTrendStrategy）

| 参数名称            | 类型  | 默认值 | 建议范围 | 步长 | 说明与作用                     |
| ------------------- | ----- | ------ | -------- | ---- | ------------------------------ |
| trend_entry_window  | int   | 40     | 20~60    | 5    | Donchian 入场通道周期          |
| trend_exit_window   | int   | 14     | 8~20     | 2    | Donchian 退出通道周期          |
| trend_adx_threshold | float | 25.0   | 20~30    | 1.0  | 趋势模块自身 ADX 要求          |
| stop_atr_multiple   | float | 3.0    | 2.0~4.5  | 0.2  | ATR 追踪止损倍数（越大越宽松） |

#### 7. 震荡模块参数（SimpleRangeStrategy）

| 参数名称                    | 类型  | 默认值 | 建议范围 | 步长 | 说明与作用                        |
| --------------------------- | ----- | ------ | -------- | ---- | --------------------------------- |
| range_boll_window           | int   | 26     | 18~40    | 2    | 布林带周期                        |
| range_zscore_entry          | float | 2.1    | 1.5~2.8  | 0.1  | 入场 z-score 极值阈值             |
| range_zscore_exit           | float | 0.45   | 0.3~0.8  | 0.05 | 退出 z-score 阈值                 |
| range_adx_ceiling           | float | 17.0   | 14~22    | 1.0  | 允许参与震荡的 ADX 上限           |
| range_min_hold_bars         | int   | 3      | 2~8      | 1    | 最小持仓K线数（防止过于频繁进出） |
| range_reentry_cooldown_bars | int   | 2      | 1~5      | 1    | 平仓后重新入场冷却周期            |

#### 8. 防守模块参数（SimpleDefenseStrategy）

| 参数名称                    | 类型  | 默认值 | 建议范围   | 步长 | 说明与作用                                   |
| --------------------------- | ----- | ------ | ---------- | ---- | -------------------------------------------- |
| defense_enable_probe        | bool  | False  | True/False | -    | 是否开启混沌期轻度试探（建议回测时打开测试） |
| defense_long_rsi_threshold  | float | 60.0   | 55~70      | 2.0  | 多头试探 RSI 阈值                            |
| defense_short_rsi_threshold | float | 40.0   | 30~45      | 2.0  | 空头试探 RSI 阈值                            |
| defense_probe_signal        | float | 0.25   | 0.15~0.40  | 0.05 | 试探信号强度（越小越保守）                   |

---

**使用建议**：

- **回测优先调优顺序**：`chaos_max_position_scale` → `entry/exit_signal_threshold` → `state_confirm_bars` → `trend_adx_threshold`
- **网格搜索推荐**：先固定大部分参数，只对以上4~5个参数做粗网格（步长较大），再细调。
- **实盘前**：把 `defense_enable_probe` 设为 True，`chaos_max_position_scale` 控制在 0.3~0.5 之间。
