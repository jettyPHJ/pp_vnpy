from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData


class V13MockStrategy(CtaTemplate):
    author = "System"
    parameters = []
    variables = []

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bar_count = 0

    def on_init(self):
        self.write_log("策略初始化")
        self.load_bar(10)

    def on_start(self):
        self.write_log("策略启动")

    def on_stop(self):
        self.write_log("策略停止")

    def on_bar(self, bar: BarData):
        if not self.trading:
            return

        self.bar_count += 1

        if self.bar_count == 1:
            # 开多 2 手：超高价保证必成 -> 触发 AGGRESSIVE_LIMIT
            self.buy(bar.close_price + 1000, 2)
            # 空头止损 1 手：100% 触发 -> 触发 STOP_TRIGGERED
            self.sell(bar.close_price + 1000, 1, stop=True)

        elif self.bar_count == 3:
            # 👈 新增：挂一个大概率被动成交的买单 (低于当前价，靠后续K线波动触发)
            # 这会触发 PASSIVE_LIMIT 分支
            self.buy(bar.close_price * 0.998, 1)

        elif self.bar_count == 5:
            # 卖出平仓 -> 触发 AGGRESSIVE_LIMIT
            self.sell(bar.close_price - 1000, 1)

        elif self.bar_count == 8:
            old_name = self.strategy_name
            self.strategy_name = "MALICIOUS_TEST_STRATEGY"
            self.buy(bar.close_price + 1000, 1)
            self.strategy_name = old_name

    def on_trade(self, trade):
        self.write_log(f"成交: {trade.direction.value} {trade.offset.value} {trade.volume}@{trade.price}")

    def on_order(self, order):
        pass

    def on_stop_order(self, stop_order):
        pass
