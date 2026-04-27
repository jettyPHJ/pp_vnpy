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
