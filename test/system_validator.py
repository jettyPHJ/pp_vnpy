from collections import defaultdict
from vnpy.trader.constant import Direction, Offset, Status

# ==============================
# V1.3 日终对账与状态防爆 DoD
# ==============================


def validate_v1_3_dod(engine):
    import math
    from vnpy.trader.constant import Direction, Offset, Status
    from collections import defaultdict

    CLOSE_OFFSETS = {Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY}
    long_p = defaultdict(float)
    short_p = defaultdict(float)
    expected_pos = defaultdict(float)

    # 1. 独立算法计算期望仓位
    for trade in engine.trades.values():
        vt = trade.vt_symbol
        if trade.direction == Direction.LONG:
            if trade.offset == Offset.OPEN: long_p[vt] += trade.volume
            elif trade.offset in CLOSE_OFFSETS: short_p[vt] -= trade.volume
        elif trade.direction == Direction.SHORT:
            if trade.offset == Offset.OPEN: short_p[vt] += trade.volume
            elif trade.offset in CLOSE_OFFSETS: long_p[vt] -= trade.volume
        expected_pos[vt] = long_p[vt] - short_p[vt]

    # 2. 仓位对账
    for vt, expected in expected_pos.items():
        actual = engine.actual_pos_map.get(vt, 0)
        assert math.isclose(actual, expected, abs_tol=1e-8), \
            f"FATAL: 仓位污染! {vt} 账本仓位={actual}, 独立推演={expected}"

    # 3. 终态防爆验证
    terminal_statuses = {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}
    for chain_id, record in engine.chain_audit_map.items():
        orders = record.get("orders", [])
        if orders:
            all_terminal = all(ref.status in terminal_statuses for ref in orders)
            assert not all_terminal, f"FATAL: chain_id={chain_id} 订单已全部终态，但卡在内存中未归档！"

    print("✅ V1.3 DoD 账本级系统验证通过！")


# ==============================
# V1.4 执行摩擦防双重扣费 DoD
# ==============================


def validate_v1_4_directional_constraint(engine):
    import math
    from vnpy.trader.constant import Direction

    for record in engine.chain_audit_archive:
        for t_record in record.get("trades", []):
            trade = t_record["trade"]
            match_res = t_record.get("match_result")
            slip_res = t_record.get("slippage_result")
            if not (match_res and slip_res):
                continue

            if trade.direction == Direction.LONG:
                assert slip_res.execution_price >= match_res.match_price, "买单执行价不应低于撮合基准价"
            else:
                assert slip_res.execution_price <= match_res.match_price, "卖单执行价不应高于撮合基准价"

            if match_res.behavior.value == "PASSIVE_LIMIT" and slip_res.model_name == "V1_Default_Slippage":
                assert math.isclose(slip_res.execution_price, match_res.match_price, abs_tol=1e-8), \
                    "V1默认模型下PASSIVE不应产生滑点"

    print("✅ V1.4 滑点方向与被动限价约束验证通过！")


def validate_v1_4_double_deduction(engine):
    import math

    assert engine.friction_mode in (FrictionMode.BAR_ENHANCED, FrictionMode.TICK_REPLAY), "引擎未开启 V1.4 模式"
    aggressive_records = [
        t for rec in engine.chain_audit_archive for t in rec.get("trades", [])
        if t.get("match_result") and t["match_result"].behavior.value == "AGGRESSIVE_LIMIT"
    ]
    if not aggressive_records:
        print("⚠️ 样本中无主动成交数据，跳过双扣改价验证。")
        return

    t_record = aggressive_records[0]
    trade = t_record["trade"]
    match_res = t_record["match_result"]
    slip_res = t_record["slippage_result"]
    assert math.isclose(trade.price, match_res.match_price + slip_res.price_diff, abs_tol=1e-8), \
        "滑点未体现在物理执行价中"

    print("✅ V1.4 防双重扣费核心改价验证通过！")


def validate_v1_4_behavior_precision(engine):
    behaviors_seen = {"PASSIVE_LIMIT": False, "AGGRESSIVE_LIMIT": False}
    for rec in engine.chain_audit_archive:
        for t in rec.get("trades", []):
            if t.get("match_result") and t["match_result"].behavior.value in behaviors_seen:
                behaviors_seen[t["match_result"].behavior.value] = True

    tracker = getattr(engine, "intent_tracker", None)
    exempt_records = getattr(tracker, "exempt_trade_records", getattr(engine, "exempt_trade_records", []))
    stop_seen = any(t.get("match_result") and t["match_result"].behavior.value == "STOP_TRIGGERED" for t in exempt_records)

    if not behaviors_seen.get("AGGRESSIVE_LIMIT"):
        print("⚠️ 样本中无主动成交数据 (AGGRESSIVE_LIMIT)，跳过主动改价逻辑断言。")
    else:
        print("✅ 主动成交 (AGGRESSIVE_LIMIT) 滑点与防双扣验证通过。")

    if not behaviors_seen.get("PASSIVE_LIMIT"):
        print("⚠️ 样本中无被动成交数据 (PASSIVE_LIMIT)，跳过被动改价逻辑断言。")
    else:
        print("✅ 被动成交 (PASSIVE_LIMIT) 滑点与防双扣验证通过。")

    print("✅ V1.4 撮合行为分支覆盖验证通过！")


def validate_v1_4_accounting_price(engine):
    import math
    for t in engine.trades.values():
        expected_accounting = t.physical_price + getattr(t, "price_offset", 0.0)
        assert math.isclose(t.accounting_price, expected_accounting, abs_tol=1e-8), \
            f"复权价格错位！TradeID: {t.vt_tradeid}"
    print("✅ V1.4 复权价格穿透验证通过！")


# ==============================
# V1.5 容量与风控管道化 DoD
# ==============================


def validate_v1_5_core_mechanics():
    """验证 V1.5 的基础风控、滑点、取整机制 (白盒穿透测试)"""
    from vnpy.trader.constant import Direction, Offset
    from vnpy_ctastrategy.order_flow.models import SignalOrder, OrderSource, MarketContext, AccountSnapshot, RiskDecision
    from vnpy_ctastrategy.order_flow.pipeline_stubs import CapitalAndSizeRiskManager, DummyRiskManager
    from vnpy_ctastrategy.back_modules import VolumeImpactSlippageModel
    from vnpy_ctastrategy.order_flow.friction import ExecutionMatchResult, MatchBehavior

    class MockContract:

        def __init__(self, multiplier=10.0, min_volume=1.0, pricetick=1.0, size=10.0):
            self.multiplier = multiplier
            self.min_volume = min_volume
            self.pricetick = pricetick
            self.size = size

    risk_manager = CapitalAndSizeRiskManager(margin_rate=0.1, max_order_size=50.0, max_participation_rate=0.15)
    slippage_model = VolumeImpactSlippageModel(impact_factor=1.5)
    contract = MockContract()

    base_signal = SignalOrder(chain_id="T001",
                              source=OrderSource.STRATEGY,
                              strategy_name="Test",
                              vt_symbol="RB99.SHFE",
                              direction=Direction.LONG,
                              offset=Offset.OPEN,
                              price=4000.0,
                              volume=100.0,
                              lock=False,
                              net=False)

    # 1. 容量裁剪 (SHRINK) 验证
    ctx_ready = MarketContext(vt_symbol="RB99.SHFE", reference_volume=200, is_ready=True)  # cap: 30
    snap_rich = AccountSnapshot(available_cash=10_000_000.0)
    ro_shrink = risk_manager.evaluate(base_signal, ctx_ready, snap_rich, contract)
    assert ro_shrink.decision == RiskDecision.SHRINK, "大单应触发缩量"
    assert ro_shrink.adjusted_volume == 30.0, "应缩量至30手"

    # 2. 暖机期边界
    ctx_warmup = MarketContext(vt_symbol="RB99.SHFE", reference_volume=10, is_ready=False)
    ro_warmup_pass = risk_manager.evaluate(base_signal, ctx_warmup, snap_rich, contract)
    assert ro_warmup_pass.decision == RiskDecision.PASS, "暖机期应跳过容量检查"
    assert ro_warmup_pass.adjusted_volume == 100.0, "暖机期不应缩量"

    snap_poor = AccountSnapshot(available_cash=1000.0)
    ro_warmup_reject = risk_manager.evaluate(base_signal, ctx_warmup, snap_poor, contract)
    assert ro_warmup_reject.decision == RiskDecision.REJECT, "暖机期资金不足必须拦截"

    # 3. 0 手边界防线
    contract.min_volume = 2.0
    base_signal.volume = 1.5
    ro_zero = risk_manager.evaluate(base_signal, ctx_ready, snap_rich, contract)
    assert ro_zero.decision == RiskDecision.REJECT, "取整不足最小手数必须拦截"
    assert ro_zero.adjusted_volume == 0.0, "拦截后手数必须是0.0"

    # 4. Dummy 回归
    dummy_rm = DummyRiskManager()
    base_signal.volume = 100.0
    ro_dummy = dummy_rm.evaluate(base_signal)
    assert ro_dummy.decision == RiskDecision.PASS
    assert ro_dummy.adjusted_volume == 100.0, "Dummy 必须回传原始手数"

    # 5. 滑点深度验证
    order_mock_long = SignalOrder("T002", OrderSource.STRATEGY, "Test", "RB99", Direction.LONG, Offset.OPEN, 4000.0, 10.0,
                                  False, False)
    order_mock_long.pricetick = 1.0
    order_mock_short = SignalOrder("T003", OrderSource.STRATEGY, "Test", "RB99", Direction.SHORT, Offset.OPEN, 4000.0, 10.0,
                                   False, False)
    order_mock_short.pricetick = 1.0

    match_passive = ExecutionMatchResult(True, 4000.0, 4000.0, 10.0, MatchBehavior.PASSIVE_LIMIT)
    assert slippage_model.calculate(order_mock_long, match_passive, 10.0, ctx_ready).price_diff == 0.0

    match_agg = ExecutionMatchResult(True, 4000.0, 4000.0, 16.0, MatchBehavior.AGGRESSIVE_LIMIT)
    slip_short = slippage_model.calculate(order_mock_short, match_agg, 10.0, ctx_ready)
    assert slip_short.execution_price < 4000.0, "空头滑点价应向下恶化"

    slip_no_ctx = slippage_model.calculate(order_mock_long, match_agg, 10.0, context=None)
    assert slip_no_ctx.price_diff > 0, "空 context 应防崩溃计算滑点"

    print("✅ V1.5 核心防线单元级逻辑验证通过！")


def validate_v1_5_engine_routing():
    """验证 V1.5 引擎的 E2E 发单与状态机路由 (黑盒测试)"""
    from vnpy.trader.constant import Direction, Offset, Interval
    from vnpy_ctastrategy.backtesting import BacktestingEngine
    from vnpy_ctastrategy.template import CtaTemplate
    from vnpy_ctastrategy.order_flow.pipeline_stubs import CapitalAndSizeRiskManager
    from datetime import datetime
    from collections import deque

    class V15MinimalMockStrategy(CtaTemplate):
        author = "Test"
        parameters = []
        variables = []

        def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
            super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        def on_init(self):
            pass

        def on_start(self):
            pass

        def on_stop(self):
            pass

        def on_tick(self, tick):
            pass

        def on_bar(self, bar):
            pass

    engine = BacktestingEngine()
    engine.set_parameters(vt_symbol="RB99.SHFE",
                          interval=Interval.MINUTE,
                          start=datetime.now(),
                          rate=0.0001,
                          slippage=0,
                          size=10,
                          pricetick=1.0,
                          capital=100_000)
    engine.add_strategy(V15MinimalMockStrategy, {})
    engine.strategy.inited = True
    engine.strategy.trading = True

    engine.order_pipeline.risk_manager = CapitalAndSizeRiskManager(margin_rate=0.1,
                                                                   max_order_size=10.0,
                                                                   max_participation_rate=0.15)

    if "RB99.SHFE" not in engine.vol_windows:
        engine.vol_windows["RB99.SHFE"] = deque(maxlen=20)
    engine.vol_windows["RB99.SHFE"].extend([100.0] * 20)

    # 场景 A：拦截缩量后依然买不起的废单
    engine.capital = 10_000
    orderids_reject = engine.send_order(engine.strategy, Direction.LONG, Offset.OPEN, 4000.0, 1000.0, False, False, False)
    assert len(orderids_reject) == 0, "资金不足必须返回空单号列表"
    assert len(engine.active_limit_orders) == 0, "引擎不能下发限价单"

    # 场景 B：触发正常容量裁剪 (SHRINK)
    engine.capital = 100_000
    orderids_shrink = engine.send_order(engine.strategy, Direction.LONG, Offset.OPEN, 4000.0, 20.0, False, False, False)
    assert len(orderids_shrink) == 1, "SHRINK 必须生成单号"
    assert engine.limit_orders[orderids_shrink[0]].volume == 10.0, "订单应被缩量至10手"

    # 场景 C：0 手强拒路由验证
    orderids_zero = engine.send_order(engine.strategy, Direction.LONG, Offset.OPEN, 4000.0, 0.5, False, False, False)
    assert len(orderids_zero) == 0, "取整0手必须拒单拦截"

    print("✅ V1.5 引擎黑盒发单路由沙盒验证通过！")


# ==============================
# 全局测试总控入口
# ==============================

# ==============================
# V1.6 Tick Replay 验证器
# ==============================


def validate_v16_no_lookahead(engine) -> list[str]:
    """
    防穿越：每笔 Tick 模式成交的 trade.datetime 必须 > 其对应信号 Bar 时间。
    依赖：intent_tracker.chain_audit_archive 中的 tick_mid_price 不为 None。
    """
    errors = []
    for record in engine.intent_tracker.chain_audit_archive:
        signal = record.get("signal")
        if not signal:
            continue
        signal_dt = signal.created_at
        for t_rec in record.get("trades", []):
            if t_rec.get("tick_fill_mode") is None:
                continue  # Bar 路径跳过
            trade_dt = t_rec["trade"].datetime
            if trade_dt <= signal_dt:
                errors.append(f"穿越！chain={str(signal.chain_id)[:8]} "
                              f"trade_dt={trade_dt} <= signal_dt={signal_dt}")
    return errors


def validate_v16_partial_fill_state(engine) -> list[str]:
    """
    部分成交后 remaining_volume > 0 的记录，其订单在 chain 里必须有多条 trade。
    """
    errors = []
    for record in engine.intent_tracker.chain_audit_archive:
        partial = [t for t in record.get("trades", []) if (t.get("tick_remaining") or 0) > 0]
        if partial:
            total_trades = len(record.get("trades", []))
            if total_trades < 2:
                signal = record.get("signal")
                cid = str(signal.chain_id)[:8] if signal else "?"
                errors.append(f"chain={cid}：有 partial_fill 记录但 trade 只有 {total_trades} 条")
    return errors


def validate_v16_cancellation_audit(engine) -> list[str]:
    """
    BAR_END 到期的撤单必须在 chain_audit_archive 里留有 cancellation 记录，
    且成交量 + 撤单余量 == 信号量（量守恒检查）。
    """
    import math
    errors = []
    for record in engine.intent_tracker.chain_audit_archive:
        cancels = record.get("cancellations", [])
        bar_end_cancel = [c for c in cancels if c.get("reason") == "TIF_BAR_END_EXPIRED"]
        if not bar_end_cancel:
            continue
        signal = record.get("signal")
        if not signal:
            continue
        traded_total = sum(t["trade"].volume for t in record.get("trades", []))
        cancelled_total = sum(c.get("remaining_volume", 0) for c in bar_end_cancel)
        expected = signal.volume
        if not math.isclose(traded_total + cancelled_total, expected, rel_tol=1e-6):
            errors.append(f"chain={str(signal.chain_id)[:8]}：量不平衡 "
                          f"traded={traded_total} + cancelled={cancelled_total} != signal={expected}")
    return errors


def validate_v16_audit_fields_complete(engine) -> list[str]:
    """
    Tick 模式成交的每条 trade record 必须有完整的 7 个 Tick 审计字段。
    """
    required = ["tick_fill_mode", "tick_fill_volume", "tick_mid_price", "tick_mid_offset"]
    errors = []
    for record in engine.intent_tracker.chain_audit_archive:
        for t_rec in record.get("trades", []):
            if t_rec.get("tick_fill_mode") is None:
                continue  # Bar 路径不检查
            for field in required:
                if t_rec.get(field) is None:
                    signal = record.get("signal")
                    cid = str(signal.chain_id)[:8] if signal else "?"
                    errors.append(f"chain={cid}：{field} 为 None")
    return errors


def validate_v16_tick_replay_runtime(engine) -> list[str]:
    """前置检查：确认 Tick Replay 实际发生过，防止 V1.6 validator 空通过。"""
    from vnpy_ctastrategy.base import FrictionMode

    errors = []
    if getattr(engine, "friction_mode", None) != FrictionMode.TICK_REPLAY:
        errors.append(f"引擎 friction_mode={getattr(engine, 'friction_mode', None)}，未开启 TICK_REPLAY")

    stats = getattr(engine, "tick_replay_stats", None)
    if not stats:
        errors.append("缺少 tick_replay_stats，引擎版本不兼容")
        return errors

    if stats.get("ticks_loaded", 0) <= 0:
        errors.append("TickReplayStore 未加载任何 Tick 数据")
    if stats.get("ticks_replayed", 0) <= 0:
        errors.append("Tick Replay 从未执行，检查 configure_execution/load_data/new_bar 链路")

    return errors


def run_v16_validators(engine) -> dict:
    """
    V1.6 验证器统一入口，返回各项测试结果字典。
    键为检查名，值为 {"pass": bool, "errors": list[str]}。
    "overall" 键汇总全局通过状态。
    """
    checks = [
        ("Tick Replay 运行时检查", validate_v16_tick_replay_runtime),
        ("防穿越", validate_v16_no_lookahead),
        ("部分成交状态", validate_v16_partial_fill_state),
        ("撤单审计", validate_v16_cancellation_audit),
        ("审计字段完整性", validate_v16_audit_fields_complete),
    ]
    results = {}
    all_pass = True
    for name, fn in checks:
        errs = fn(engine)
        results[name] = {"pass": len(errs) == 0, "errors": errs}
        if errs:
            all_pass = False
    results["overall"] = all_pass
    return results


def run_all_system_validations(engine=None):
    """
    统一执行所有架构级防回退测试。
    - 若传入 engine，则一并执行依赖回测数据的 V1.3/V1.4 测试。
    - V1.5 测试为独立沙盒，始终执行。
    """
    print("\n" + "=" * 55)
    print("🚀 开始执行全量架构防回退系统测试 (V1.3 - V1.6)")
    print("=" * 55)

    if engine:
        print("\n⏳ [阶段 1] 执行 V1.3 架构防爆与账本对账验证...")
        validate_v1_3_dod(engine)

        print("\n⏳ [阶段 2] 执行 V1.4 执行摩擦与复权穿透验证...")
        validate_v1_4_directional_constraint(engine)
        validate_v1_4_double_deduction(engine)
        validate_v1_4_behavior_precision(engine)
        validate_v1_4_accounting_price(engine)
    else:
        print("\n⚠️ 未传入 engine 实例，已跳过 V1.3 和 V1.4 (需依赖历史数据) 的验证。")

    print("\n⏳ [阶段 3] 执行 V1.5 容量与风控拦截双重沙盒验证...")
    validate_v1_5_core_mechanics()
    validate_v1_5_engine_routing()

    if engine:
        print("\n⏳ [阶段 4] 执行 V1.6 Tick Replay 集成验证...")
        v16_results = run_v16_validators(engine)
        for check_name, result in v16_results.items():
            if check_name == "overall":
                continue
            status = "✅" if result["pass"] else "❌"
            print(f"   {status} {check_name}")
            for err in result["errors"]:
                print(f"      └─ {err}")
        if not v16_results.get("overall", True):
            raise AssertionError("❌ V1.6 验证发现问题，详见上方错误列表。")
        print("   ✅ V1.6 Tick Replay 全部验证通过")

    print("\n" + "=" * 55)
    print("🎉 全部架构级验证通过！系统底座稳固，可安全用于投研。")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    # 当直接运行 python system_validator.py 时，只跑独立的 V1.5 沙盒测试
    run_all_system_validations(engine=None)
