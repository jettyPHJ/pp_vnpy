import importlib
from enum import Enum
from datetime import datetime

from vnpy.trader.constant import Interval
from vnpy_ctastrategy.backtesting import BacktestingMode

# ==============================================================
# 1. 🌍 全局回测环境配置 (扁平化常量)
# ==============================================================
AUTO_DOWNLOAD = True
START_DATE = datetime(2024, 11, 1)
END_DATE = datetime(2026, 4, 15)
INTERVAL = Interval.DAILY
MODE = BacktestingMode.BAR
CAPITAL = 1_000_000

RATE = 0.0001
BY_VOLUME = False
SLIPPAGE = 1.0
SIZE = 10
PRICETICK = 1.0
WARMUP_DAYS = 120

PHYSICAL_SYMBOLS = [
    "rb2405.SHFE",
    "rb2410.SHFE",
    "rb2501.SHFE",
    "rb2505.SHFE",
    "rb2510.SHFE",
    "rb2601.SHFE",
    "rb2605.SHFE",
    "rb2610.SHFE",
]


# ==============================================================
# 2. 🎛️ 策略路由枚举
# ==============================================================
class StrategyMode(Enum):
    DONCHIAN = "DonchianChannelStrategy"  # 唐奇安通道策略
    TEST_HOLD = "TestRolloverStrategy"  # 换月死扛测试策略


# 🔴 在这里切换你要运行的策略
CURRENT_STRATEGY = StrategyMode.TEST_HOLD

# ==============================================================
# 3. 🎯 各策略独立配置区
# ==============================================================

# --- 策略 1: 唐奇安通道 ---
DONCHIAN_CONFIG = {
    "module_path": "vnpy_ctastrategy.strategies.donchian_channel_strategy",
    "vt_symbol": "rb888.SHFE",
    "parameters": {
        "entry_window": 5,  # 突破入场通道周期
        "exit_window": 2,  # 跌破出场通道周期
        "fixed_size": 1,  # 单次开仓手数
        "long_only": False  # True=仅做多，False=双向
    }
}

# --- 策略 2: 换月死扛 ---
TEST_HOLD_CONFIG = {
    "module_path": "vnpy_ctastrategy.strategies.test",
    "vt_symbol": "rb888.SHFE",
    "parameters": {
        "fixed_size": 1
    }
}

STRATEGY_ROUTES = {
    StrategyMode.DONCHIAN: DONCHIAN_CONFIG,
    StrategyMode.TEST_HOLD: TEST_HOLD_CONFIG,
}


# ==============================================================
# 🛡️ 自动化参数审计
# ==============================================================
def __auto_verify_on_load():
    route = STRATEGY_ROUTES.get(CURRENT_STRATEGY)
    if not route:
        raise ValueError(f"未找到当前策略路由配置: {CURRENT_STRATEGY}")

    module = importlib.import_module(route["module_path"])
    strategy_class = getattr(module, CURRENT_STRATEGY.value)

    source_params = list(getattr(strategy_class, "parameters", []))
    config_params = list(route["parameters"].keys())

    # 一般不把 author 当成真正需要传入的参数
    source_params = [p for p in source_params if p != "author"]

    missing = [p for p in source_params if p not in config_params]
    extra = [p for p in config_params if p not in source_params]

    print("-" * 50)
    print(f"🔍 正在执行配置审计: {CURRENT_STRATEGY.value}")

    if missing:
        raise ValueError(f"config.py 缺失源码要求的参数: {missing}")

    if extra:
        raise ValueError(f"config.py 包含策略源码中未定义的参数: {extra}")

    print("✅ 配置验证通过：参数列表与策略定义完美匹配。")
    print("-" * 50)


__auto_verify_on_load()
