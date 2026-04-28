from collections import defaultdict
from vnpy.trader.constant import Direction, Offset, Status


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
        # [核心修复] 物理仓位的推演必须包含所有成交（包括被豁免追踪的止损单）
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

    print("✅ V1.3 DoD 系统级验证通过！")


# ==============================
# V1.4 Execution Friction DoD
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

    assert getattr(engine, "friction_mode", "legacy") == "v1.4", "引擎未开启 V1.4 模式"
    aggressive_records = [
        t for rec in engine.chain_audit_archive for t in rec.get("trades", [])
        if t.get("match_result") and t["match_result"].behavior.value == "AGGRESSIVE_LIMIT"
    ]
    if not aggressive_records:
        print("⚠️ 样本中无主动成交数据，跳过改价验证。")
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

    assert behaviors_seen["PASSIVE_LIMIT"], "未验证到 PASSIVE_LIMIT 分支"
    assert behaviors_seen["AGGRESSIVE_LIMIT"], "未验证到 AGGRESSIVE_LIMIT 分支"
    assert stop_seen, "未验证到 STOP_TRIGGERED 分支"

    print("✅ V1.4 撮合行为分支覆盖验证通过！")


def validate_v1_4_accounting_price(engine):
    import math

    for t in engine.trades.values():
        expected_accounting = t.physical_price + getattr(t, "price_offset", 0.0)
        assert math.isclose(t.accounting_price, expected_accounting, abs_tol=1e-8), \
            f"复权价格错位！TradeID: {t.vt_tradeid}"

    print("✅ V1.4 复权价格穿透验证通过！")
