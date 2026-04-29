"""
PipelineStressTestStrategy — 端到端管道全量压测策略
=====================================================

设计目标
--------
在单次真实回测中，系统性地触发以下所有关键测试节点，
确保 V1.3 ~ V1.5 全部验证器都能拿到真实引擎数据运行：

  ┌─────────────────────────────────────────────────────────┐
  │ Phase │ Bar  │ 测试点                    │ 验证器       │
  ├───────┼──────┼───────────────────────────┼─────────────┤
  │  P1   │  2   │ AGGRESSIVE_LIMIT 买入      │ V1.4        │
  │  P2   │  5   │ PASSIVE_LIMIT 挂单(等触发) │ V1.4        │
  │  P3   │  12  │ STOP_TRIGGERED 止损平多    │ V1.4        │
  │  P4   │  18  │ SHRINK 超大单容量裁剪      │ V1.5 黑盒   │
  │  P5   │  22  │ REJECT HARD_LIMIT 恶意名   │ V1.5 黑盒   │
  │  P6   │  28  │ AGGRESSIVE_LIMIT 做空      │ V1.4        │
  │  P7   │  35  │ STOP_TRIGGERED 止损平空    │ V1.4        │
  │  P8   │  40+ │ 全清仓兜底(保证账本闭合)  │ V1.3        │
  └─────────────────────────────────────────────────────────┘

V1.4 撮合行为规则（来自 back_modules.py）
  - AGGRESSIVE_LIMIT：order.price >= open_price → 以开盘价立即成交 + 滑点恶化
  - PASSIVE_LIMIT   ：low <= order.price < open_price → 以限价被动成交 + 无滑点
  - STOP_TRIGGERED  ：stop=True 的停止单触发 → 独立记录，豁免追踪链

设计约束
  - 仅使用 fixed_size=1 手，避免保证金不足导致 Phase 4 SHRINK 之外的意外 REJECT
  - Phase 2 被动挂单价格 = close * 0.990，日线行情通常会在若干 bar 内触及
  - Phase 4 故意发 200 手，远超 max_order_size=50，必然触发 SHRINK → 裁剪至容量上限
  - Phase 5 伪造 MALICIOUS_TEST_STRATEGY 名称，DummyRiskManager 的 reject_keywords 必然拦截
"""

from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData


class PipelineStressTestStrategy(CtaTemplate):
    author = "PipelineStressTester"

    # ── 参数 ────────────────────────────────────────────────────────────────
    fixed_size: int = 1  # 正常交易手数；Phase 4 固定发 200 手测试 SHRINK

    parameters = ["fixed_size"]
    variables = []

    # ── 内部状态 ─────────────────────────────────────────────────────────────
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bar_count: int = 0
        self._done: set = set()  # 已执行的 Phase 标记，防重入

    # ── 生命周期 ─────────────────────────────────────────────────────────────
    def on_init(self):
        self.write_log("【压测策略】初始化，加载预热数据")
        self.load_bar(10)

    def on_start(self):
        self.write_log("【压测策略】启动，即将系统性触发所有管道测试节点")

    def on_stop(self):
        self.write_log(f"【压测策略】停止，共运行 {self.bar_count} 根 Bar，最终仓位={self.pos}")

    # ── 核心逻辑 ─────────────────────────────────────────────────────────────
    def on_bar(self, bar: BarData):
        if not self.trading:
            return

        self.bar_count += 1
        bc = self.bar_count

        # ═══════════════════════════════════════════════════════════════════
        # Phase 1 | Bar 2 | AGGRESSIVE_LIMIT 买入
        # ───────────────────────────────────────────────────────────────────
        # 价格 = close + 2000，远超任何合理开盘价 → order.price >= open_price
        # → match_limit_order_v14 判定为 AGGRESSIVE_LIMIT，以开盘价成交
        # ═══════════════════════════════════════════════════════════════════
        if bc == 2 and "P1" not in self._done:
            self._done.add("P1")
            aggressive_price = bar.close_price + 2000
            self.write_log(f"[P1 | Bar {bc}] AGGRESSIVE_LIMIT 买入 "
                           f"@ {aggressive_price:.1f}（close+2000）")
            self.buy(aggressive_price, self.fixed_size)

        # ═══════════════════════════════════════════════════════════════════
        # Phase 2 | Bar 5 | PASSIVE_LIMIT 被动挂单
        # ───────────────────────────────────────────────────────────────────
        # 价格 = close * 0.990，低于市价1%。
        # 日线回测中，bar 的 low 通常会在几根 bar 内触及此价 →
        # 成交时 low <= order.price < open_price → PASSIVE_LIMIT
        # ═══════════════════════════════════════════════════════════════════
        elif bc == 5 and "P2" not in self._done:
            self._done.add("P2")
            passive_price = round(bar.close_price * 0.990, 0)
            self.write_log(f"[P2 | Bar {bc}] PASSIVE_LIMIT 挂单买入 "
                           f"@ {passive_price:.1f}（close*0.990），等待市价下触")
            self.buy(passive_price, self.fixed_size)

        # ═══════════════════════════════════════════════════════════════════
        # Phase 3 | Bar 12 | STOP_TRIGGERED 止损平多仓
        # ───────────────────────────────────────────────────────────────────
        # 若 Phase 1 / Phase 2 已有持仓，发出向下止损单 → STOP_TRIGGERED
        # 止损单通过 record_standalone_trade 豁免追踪链，属于独立成交记录
        # ═══════════════════════════════════════════════════════════════════
        elif bc == 12 and "P3" not in self._done:
            self._done.add("P3")
            if self.pos > 0:
                stop_price = round(bar.close_price * 0.985, 0)
                self.write_log(f"[P3 | Bar {bc}] STOP_TRIGGERED 止损平多 "
                               f"@ {stop_price:.1f}（close*0.985），pos={self.pos}")
                self.sell(stop_price, abs(self.pos), stop=True)
            else:
                self.write_log(f"[P3 | Bar {bc}] 无多头持仓（P1/P2 尚未成交），"
                               f"改用主动卖出兜底以保持账本平衡")

        # ═══════════════════════════════════════════════════════════════════
        # Phase 4 | Bar 18 | SHRINK 超大单容量裁剪
        # ───────────────────────────────────────────────────────────────────
        # 发出 200 手，CapitalAndSizeRiskManager 的 max_order_size=50 必然触发
        # SHRINK 决策，裁剪后以实际可下数量报单（约 50 手或参与率上限）
        # ═══════════════════════════════════════════════════════════════════
        elif bc == 18 and "P4" not in self._done:
            self._done.add("P4")
            aggressive_price = bar.close_price + 2000
            self.write_log(f"[P4 | Bar {bc}] SHRINK 测试：发出 200 手超大单 "
                           f"@ {aggressive_price:.1f}，期待被裁剪至≤50手")
            self.buy(aggressive_price, 200)  # 200 >> max_order_size=50 → SHRINK

        # ═══════════════════════════════════════════════════════════════════
        # Phase 5 | Bar 22 | REJECT HARD_LIMIT 恶意策略名拦截
        # ───────────────────────────────────────────────────────────────────
        # DummyRiskManager 的 reject_keywords={"MALICIOUS_TEST"} 拦截含该字段的策略名
        # 伪造名称后发单 → 必然触发 REJECT，订单不进入 active_limit_orders
        # ═══════════════════════════════════════════════════════════════════
        elif bc == 22 and "P5" not in self._done:
            self._done.add("P5")
            old_name = self.strategy_name
            self.strategy_name = "MALICIOUS_TEST_STRATEGY"
            self.write_log(f"[P5 | Bar {bc}] REJECT 测试：伪造策略名 '{self.strategy_name}'，"
                           f"期待 DummyRiskManager 硬拒单")
            self.buy(bar.close_price + 1000, self.fixed_size)
            self.strategy_name = old_name  # 立即还原，不污染后续交易

        # ═══════════════════════════════════════════════════════════════════
        # Phase 6 | Bar 28 | AGGRESSIVE_LIMIT 做空（空头 AGGRESSIVE）
        # ───────────────────────────────────────────────────────────────────
        # 只在无仓时开空，价格远低于市价 → order.price <= open_price → AGGRESSIVE
        # 提供第二笔 AGGRESSIVE_LIMIT 记录，覆盖空头方向的滑点方向验证
        # ═══════════════════════════════════════════════════════════════════
        elif bc == 28 and "P6" not in self._done:
            self._done.add("P6")
            if self.pos == 0:
                aggressive_short = bar.close_price - 2000
                self.write_log(f"[P6 | Bar {bc}] AGGRESSIVE_LIMIT 做空 "
                               f"@ {aggressive_short:.1f}（close-2000）")
                self.short(aggressive_short, self.fixed_size)
            else:
                self.write_log(f"[P6 | Bar {bc}] 当前仓位={self.pos}，跳过做空，"
                               f"改为直接清仓")
                if self.pos > 0:
                    self.sell(bar.close_price - 2000, abs(self.pos))

        # ═══════════════════════════════════════════════════════════════════
        # Phase 7 | Bar 35 | STOP_TRIGGERED 止损平空（第二次止损记录）
        # ───────────────────────────────────────────────────────────────────
        # 为空头持仓发出向上止损单 → STOP_TRIGGERED（空头方向）
        # ═══════════════════════════════════════════════════════════════════
        elif bc == 35 and "P7" not in self._done:
            self._done.add("P7")
            if self.pos < 0:
                stop_cover_price = round(bar.close_price * 1.015, 0)
                self.write_log(f"[P7 | Bar {bc}] STOP_TRIGGERED 止损平空 "
                               f"@ {stop_cover_price:.1f}（close*1.015），pos={self.pos}")
                self.cover(stop_cover_price, abs(self.pos), stop=True)
            else:
                self.write_log(f"[P7 | Bar {bc}] 无空头持仓，跳过空头止损测试")

        # ═══════════════════════════════════════════════════════════════════
        # Phase 8 | Bar 40+ | 全清仓兜底（保障 V1.3 账本闭合验证通过）
        # ───────────────────────────────────────────────────────────────────
        # 确保回测结束时仓位归零，防止 V1.3 DoD 因残仓导致账本不平而失败
        # ═══════════════════════════════════════════════════════════════════
        elif bc >= 40 and self.pos != 0 and "P8" not in self._done:
            self._done.add("P8")
            self.write_log(f"[P8 | Bar {bc}] 全清仓兜底，当前仓位={self.pos}")
            if self.pos > 0:
                self.sell(bar.close_price - 2000, abs(self.pos))
            elif self.pos < 0:
                self.cover(bar.close_price + 2000, abs(self.pos))

    # ── 回调 ─────────────────────────────────────────────────────────────────
    def on_trade(self, trade):
        self.write_log(f"  ✅ 成交 | {trade.direction.value} {trade.offset.value} "
                       f"{trade.volume}手 @ {trade.price:.1f} | 当前仓位={self.pos}")

    def on_order(self, order):
        pass

    def on_stop_order(self, stop_order):
        pass
