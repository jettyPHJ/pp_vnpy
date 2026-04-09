from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager
)

import numpy as np
import pandas as pd


class MeanReversionGridStrategyAligned(CtaTemplate):
    """
    尽量与 QMT 版本对齐的 vn.py 策略

    对齐点：
    1. 使用最近 300 根收盘价
    2. mean 用 pandas mean
    3. std 用 pandas std(ddof=1)，与 QMT 的 Series.std() 一致
    4. 通道乘以 sensitive
    5. grid 逻辑与 pd.cut 尽量一致
    6. 目标仓位 = base_capital * weight / (margin_ratio * price * contract_multiplier)
    7. 净持仓模型：多为正，空为负，空仓为 0
    8. 调仓时显式先平后开，减少隐式行为差异
    """

    author = "User"

    # ===== 核心参数：按 QMT 对齐 =====
    window_size = 300
    sensitive = 1

    # [强空, 弱空, 空仓, 弱多, 强多]
    weight_strong_short = 0.25
    weight_weak_short = 0.15
    weight_flat = 0.0
    weight_weak_long = 0.15
    weight_strong_long = 0.25

    # QMT 对齐参数
    base_capital = 1000000
    margin_ratio = 0.05
    contract_multiplier = 10

    # 下单价格偏移（为了尽量提高 bar 回测成交概率）
    # 多头买/平空时加价；空头卖/开空时减价
    price_tick_add = 0

    parameters = [
        "window_size",
        "sensitive",
        "weight_strong_short",
        "weight_weak_short",
        "weight_flat",
        "weight_weak_long",
        "weight_strong_long",
        "base_capital",
        "margin_ratio",
        "contract_multiplier",
        "price_tick_add",
    ]

    variables = [
        "grid_level",
        "target_pos",
        "mean_price",
        "std_price",
        "upper_band",
        "lower_band",
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=self.window_size + 20)

        self.grid_level = -1
        self.target_pos = 0
        self.mean_price = 0.0
        self.std_price = 0.0
        self.upper_band = 0.0
        self.lower_band = 0.0
        self.trading_ready = False

        # grid -> 权重映射，与 QMT 完全同向
        # 0: 强多, 1: 弱多, 2: 空仓, 3: 弱空, 4: 强空
        self.w_map = {
            0: self.weight_strong_long,
            1: self.weight_weak_long,
            2: self.weight_flat,
            3: self.weight_weak_short,
            4: self.weight_strong_short,
        }

    def on_init(self):
        self.write_log("策略初始化")
        self.load_bar(days=20)
        self.trading_ready = True
        self.put_event()

    def on_start(self):
        self.write_log("策略启动")
        self.put_event()

    def on_stop(self):
        self.write_log("策略停止")
        self.put_event()

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def _calc_grid(self, price: float, mean_val: float, std_val: float) -> int:
        """
        尽量复刻 QMT:
        band = mean + [-inf, -3, -2, 2, 3, inf] * std * sensitive
        pd.cut(price, band, labels=[0,1,2,3,4])

        pd.cut 默认：
        - right=True
        - 区间为 (a, b]
        """
        bands = [
            -np.inf,
            mean_val - 3 * std_val * self.sensitive,
            mean_val - 2 * std_val * self.sensitive,
            mean_val + 2 * std_val * self.sensitive,
            mean_val + 3 * std_val * self.sensitive,
            np.inf,
        ]

        # 为了最大程度贴近 QMT 的 pd.cut，直接复用 pandas
        grid_res = pd.cut([price], bins=bands, labels=[0, 1, 2, 3, 4])
        if pd.isna(grid_res[0]):
            return -1
        return int(grid_res[0])

    def _calc_target_pos(self, grid: int, price: float) -> int:
        """
        与 QMT 对齐：
        grid 0/1 -> 多仓
        grid 3/4 -> 空仓
        grid 2   -> 空仓

        target = int(base_capital * w / (margin_ratio * price * multiplier))
        """
        if price <= 0:
            return 0

        w = self.w_map.get(grid, 0.0)

        if self.margin_ratio <= 0 or self.contract_multiplier <= 0:
            return 0

        target_value = self.base_capital * w
        qty = int(target_value / (self.margin_ratio * price * self.contract_multiplier))

        if grid <= 1:
            return qty
        elif grid >= 3:
            return -qty
        else:
            return 0

    def _get_order_prices(self, bar: BarData):
        try:
            tick = self.get_pricetick()
        except Exception:
            tick = 0

        if not tick or tick <= 0:
            tick = 1

        buy_price = float(bar.close_price + self.price_tick_add * tick)
        short_price = float(bar.close_price - self.price_tick_add * tick)

        cover_price = buy_price
        sell_price = short_price

        return buy_price, sell_price, short_price, cover_price

    def _rebalance_position(self, bar: BarData, target_pos: int):
        """
        显式先平后开，尽量贴近 QMT 的四种动作：
        - 多头增加: buy open
        - 多头减少: sell close
        - 空头增加: short open
        - 空头减少: cover close

        净持仓表示：
        pos > 0: 多头
        pos < 0: 空头
        """
        current_pos = self.pos
        buy_price, sell_price, short_price, cover_price = self._get_order_prices(bar)

        # 已经一致
        if target_pos == current_pos:
            return

        # ===== 目标更大：往多头方向移动 =====
        if target_pos > current_pos:
            # 先平空
            if current_pos < 0:
                cover_qty = min(abs(current_pos), target_pos - current_pos)
                if cover_qty > 0:
                    self.cover(cover_price, cover_qty)
                    self.write_log(
                        f"平空 {cover_qty} 手 | current_pos={current_pos}, target_pos={target_pos}"
                    )
                    return

            # 再开多
            open_long_qty = target_pos - max(current_pos, 0)
            if open_long_qty > 0:
                self.buy(buy_price, open_long_qty)
                self.write_log(
                    f"开多 {open_long_qty} 手 | current_pos={current_pos}, target_pos={target_pos}"
                )
                return

        # ===== 目标更小：往空头方向移动 =====
        if target_pos < current_pos:
            # 先平多
            if current_pos > 0:
                sell_qty = min(current_pos, current_pos - target_pos)
                if sell_qty > 0:
                    self.sell(sell_price, sell_qty)
                    self.write_log(
                        f"平多 {sell_qty} 手 | current_pos={current_pos}, target_pos={target_pos}"
                    )
                    return

            # 再开空
            open_short_qty = abs(target_pos - min(current_pos, 0))
            if open_short_qty > 0:
                self.short(short_price, open_short_qty)
                self.write_log(
                    f"开空 {open_short_qty} 手 | current_pos={current_pos}, target_pos={target_pos}"
                )
                return

    def on_bar(self, bar: BarData):
        # 建议每根 bar 先撤销旧单，避免委托残留影响回测一致性
        self.cancel_all()

        self.am.update_bar(bar)
        if not self.am.inited:
            return

        close_array = self.am.close[-self.window_size:]
        if len(close_array) < self.window_size:
            return

        close_series = pd.Series(close_array).dropna()
        if len(close_series) < self.window_size:
            return

        # ===== 与 QMT 对齐：mean + pandas std(ddof=1) =====
        mean_val = float(close_series.mean())
        std_val = float(close_series.std(ddof=1))

        if std_val == 0 or np.isnan(std_val):
            std_val = 0.001

        self.mean_price = mean_val
        self.std_price = std_val

        self.lower_band = mean_val - 2 * std_val * self.sensitive
        self.upper_band = mean_val + 2 * std_val * self.sensitive

        current_price = float(bar.close_price)

        # ===== grid 判定 =====
        grid = self._calc_grid(current_price, mean_val, std_val)
        if grid < 0:
            return

        self.grid_level = grid

        # ===== 目标仓位 =====
        target_pos = self._calc_target_pos(grid, current_price)
        self.target_pos = target_pos

        # ===== 调试日志 =====
        self.write_log(
            f"[STAT] price={current_price:.4f}, mean={mean_val:.4f}, std={std_val:.4f}, "
            f"grid={grid}, target_pos={target_pos}, current_pos={self.pos}"
        )

        if not self.trading_ready:
            return
        # ===== 调仓 =====
        self._rebalance_position(bar, target_pos)

        self.put_event()

    def on_order(self, order: OrderData):
        pass

    def on_trade(self, trade: TradeData):
        self.write_log(
            f"成交：direction={trade.direction.value}, offset={trade.offset.value}, "
            f"volume={trade.volume}, price={trade.price}"
        )
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder):
        pass