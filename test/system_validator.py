from collections import defaultdict
from vnpy.trader.constant import Direction, Offset, Status


def validate_v1_3_dod(engine):
    """
    V1.3/V1.4 强制契约校验：在回测或实盘结束时调用
    验证 Execution Fidelity，确保仓位无污染且内存不泄漏。
    """
    print("\n[系统自检] 开始执行 V1.3/V1.4 架构契约验证...")

    # ---------------------------------------------------------
    # 校验 1: 真实的实盘级仓位推演 (严格区分多空与开平方向)
    # ---------------------------------------------------------
    long_pos_map = defaultdict(int)
    short_pos_map = defaultdict(int)

    for trade in engine.get_all_trades():
        vol = trade.volume
        # 多头方向交易
        if trade.direction == Direction.LONG:
            if trade.offset == Offset.OPEN:
                long_pos_map[trade.vt_symbol] += vol  # 多开：加多仓
            else:
                short_pos_map[trade.vt_symbol] -= vol  # 空平：减空仓
        # 空头方向交易
        elif trade.direction == Direction.SHORT:
            if trade.offset == Offset.OPEN:
                short_pos_map[trade.vt_symbol] += vol  # 空开：加空仓
            else:
                long_pos_map[trade.vt_symbol] -= vol  # 多平：减多仓

    # 对账：推导实际净仓位
    for vt_symbol, current_net_pos in engine.actual_pos_map.items():
        expected_net_pos = long_pos_map[vt_symbol] - short_pos_map[vt_symbol]
        assert current_net_pos == expected_net_pos, \
            f"FATAL: 仓位污染! {vt_symbol} 引擎仓位={current_net_pos}, " \
            f"流水推演=(多{long_pos_map[vt_symbol]} - 空{short_pos_map[vt_symbol]})={expected_net_pos}。存在违规篡改!"

    # ---------------------------------------------------------
    # 校验 5: 终态转移与内存防爆检查 (Lifecycle Terminal Check)
    # ---------------------------------------------------------
    TERMINAL_STATUSES = {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}

    for chain_id, audit_record in engine.chain_audit_map.items():
        # 获取该意图链下的所有物理订单状态
        child_orders = audit_record.get("orders", [])

        if not child_orders:
            continue

        # 检查是否所有子订单都已进入终态
        all_terminal = all(order.status in TERMINAL_STATUSES for order in child_orders)

        # 【架构红线】进入绝对终态的意图，必须被清理并落盘，不应驻留在活跃字典中
        assert not all_terminal, \
            f"FATAL: 内存泄漏隐患! chain_id [{chain_id}] 的所有订单均已进入终态 " \
            f"(ALLTRADED/CANCELLED/REJECTED)，但仍驻留在活跃内存字典中，未被归档机制回收！"

    print("✅ 恭喜：系统通过 Execution Fidelity 验证。状态机推演与内存回收机制完全健康！")
