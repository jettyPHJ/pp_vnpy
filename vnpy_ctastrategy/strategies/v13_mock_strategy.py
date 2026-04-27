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
        # 👇 核心修复：拦截暖机期的 K 线，只在正式回测时计数和发单
        if not self.trading:
            return

        self.bar_count += 1

        if self.bar_count == 1:
            # 开多 2 手：超高价保证必成 -> 走 PIPELINE
            self.buy(bar.close_price + 1000, 2)

            # 空头止损 1 手：设在极高价，下一根K线必然跌破，100% 触发 -> 走 EXEMPT
            self.sell(bar.close_price + 1000, 1, stop=True)

        elif self.bar_count == 5:
            # 卖出 1 手平仓：超低价保证必成 -> 走 PIPELINE
            # 累计：多2、止损空1、普通空1，仓位精确归 0
            self.sell(bar.close_price - 1000, 1)

        elif self.bar_count == 8:
            # 风控阻断：修改名称触发 DummyRiskManager 的拦截 -> 走 RISK_REJECT
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
