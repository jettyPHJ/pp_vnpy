from collections import defaultdict
from vnpy.trader.constant import Direction, Offset
from vnpy.trader.object import TradeData

CLOSE_OFFSETS = {Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY}


class PositionLedger:
    """真实仓位账本：actual_pos_map 是唯一真相来源"""

    def __init__(self) -> None:
        self._long_pos: dict[str, float] = defaultdict(float)
        self._short_pos: dict[str, float] = defaultdict(float)
        # 注意约定：此处的 key 必须统一为物理合约 (如 rb2410.SHFE)
        self.actual_pos_map: dict[str, float] = defaultdict(float)

    def apply_trade(self, trade: TradeData) -> float:
        vt_symbol = trade.vt_symbol

        if trade.direction == Direction.LONG:
            if trade.offset == Offset.OPEN:
                self._long_pos[vt_symbol] += trade.volume  # 开多
            elif trade.offset in CLOSE_OFFSETS:
                self._short_pos[vt_symbol] -= trade.volume  # 平空
        elif trade.direction == Direction.SHORT:
            if trade.offset == Offset.OPEN:
                self._short_pos[vt_symbol] += trade.volume  # 开空
            elif trade.offset in CLOSE_OFFSETS:
                self._long_pos[vt_symbol] -= trade.volume  # 平多

        self.actual_pos_map[vt_symbol] = self._long_pos[vt_symbol] - self._short_pos[vt_symbol]
        return self.actual_pos_map[vt_symbol]
