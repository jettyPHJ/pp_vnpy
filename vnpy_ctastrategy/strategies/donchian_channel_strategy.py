from vnpy.trader.constant import Interval
from vnpy.trader.utility import ArrayManager
from vnpy.trader.object import TickData, BarData, TradeData
from vnpy_ctastrategy import CtaTemplate


class DonchianChannelStrategy(CtaTemplate):
    """
    唐奇安通道突破策略（V1 引擎 Phase 1 基准测试专用）
    - 特性1：严格切片剔除当前 Bar 数据，防止未来函数污染。
    - 特性2：支持 long_only 模式切换，便于分步隔离压测引擎撮合。
    - 特性3：调用底层 high_array/low_array，保证内存指针与版本兼容性最优化。
    """
    author = "V1 Engine Tester"

    # ==================== 策略参数 ====================
    entry_window: int = 20  # 入场通道周期
    exit_window: int = 10  # 出场通道周期
    fixed_size: int = 1  # 单次开仓手数
    long_only: bool = True  # 默认开启单向，防止同 Bar 极值双向触发的撮合歧义

    # ==================== 策略变量 ====================
    upper_band: float = 0.0
    lower_band: float = 0.0
    long_exit: float = 0.0
    short_exit: float = 0.0

    parameters = ["entry_window", "exit_window", "fixed_size", "long_only"]
    variables = ["upper_band", "lower_band", "long_exit", "short_exit"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # 此处仅为占位初始化，on_init 中会用真实的 buffer_size 覆盖
        self.am = ArrayManager(100)

    def on_init(self):
        """策略初始化"""
        self.write_log("策略初始化开始...")

        # 留足数据冗余余量，防止节假日/停牌导致初始化边界不齐
        buffer_size = max(self.entry_window, self.exit_window) + 50
        self.am = ArrayManager(buffer_size)

        self.load_bar(buffer_size, interval=Interval.DAILY)
        self.write_log(f"策略初始化完成，历史数据缓存容量 = {buffer_size}")

    def on_start(self):
        self.write_log("策略启动")

    def on_stop(self):
        self.write_log("策略停止")

    def on_tick(self, tick: TickData):
        pass

    def on_bar(self, bar: BarData):
        """每根 Bar 的核心逻辑"""
        self.cancel_all()

        am = self.am
        am.update_bar(bar)

        if not am.inited:
            return

        # 提取底层 numpy 数组，确保切片稳定且不受属性拦截器影响
        high_array = am.high_array
        low_array = am.low_array

        # 计算通道 (严格使用 [-N-1:-1] 剔除当前 bar，防未来函数)
        self.upper_band = high_array[-self.entry_window - 1:-1].max()
        self.lower_band = low_array[-self.entry_window - 1:-1].min()
        self.long_exit = low_array[-self.exit_window - 1:-1].min()
        self.short_exit = high_array[-self.exit_window - 1:-1].max()

        # 发出交易信号 (使用 stop=True 的停止单)
        if self.pos == 0:
            self.buy(self.upper_band, self.fixed_size, stop=True)
            if not self.long_only:
                self.short(self.lower_band, self.fixed_size, stop=True)

        elif self.pos > 0:
            self.sell(self.long_exit, abs(self.pos), stop=True)

        elif self.pos < 0:
            self.cover(self.short_exit, abs(self.pos), stop=True)

        # 打印详尽对账日志
        self.write_log(f"[{bar.datetime.strftime('%Y-%m-%d')}] 收盘={bar.close_price:.2f} | "
                       f"上轨={self.upper_band:.2f} 下轨={self.lower_band:.2f} | "
                       f"多平={self.long_exit:.2f} 空平={self.short_exit:.2f} | 仓位={self.pos}")
        self.put_event()

    def on_trade(self, trade: TradeData):
        self.write_log(f"💰 真实成交 | {trade.direction.value} {trade.offset.value} | "
                       f"数量: {trade.volume} | 价格: {trade.price:.2f} | 订单号: {trade.orderid}")
