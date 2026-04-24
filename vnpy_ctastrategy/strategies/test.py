from vnpy.trader.constant import Interval
from vnpy_ctastrategy import CtaTemplate, BarData


class TestRolloverStrategy(CtaTemplate):
    author = "Test"

    # ==================== 策略参数 ====================
    fixed_size: int = 1  # 单次开仓手数

    parameters = ["fixed_size"]

    def on_init(self):
        self.write_log("策略初始化")
        self.load_bar(10, interval=Interval.DAILY)

    def on_start(self):
        self.write_log("策略启动")

    def on_bar(self, bar: BarData):
        # 只要没有持仓，就无脑买入 1 手，然后一直死扛
        if self.pos == 0:
            self.buy(bar.close_price, 1)
