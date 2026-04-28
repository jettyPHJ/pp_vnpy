import sys
import os
import importlib
from datetime import timedelta

# 保证项目根目录在 sys.path 中
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config as cfg
from vnpy_ctastrategy.backtesting import BacktestingEngine
from report_builder import generate_web_report

from vnpy.trader.object import HistoryRequest
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange
from system_validator import (validate_v1_3_dod, validate_v1_4_directional_constraint, validate_v1_4_double_deduction,
                              validate_v1_4_behavior_precision, validate_v1_4_accounting_price)
from vnpy_ctastrategy.strategies.v13_mock_strategy import V13MockStrategy


def download_data(route: dict) -> None:
    """根据配置同步历史数据。
    对当前连续合约框架，只强制下载物理合约数据；
    连续符号 rb888.SHFE 由 ContinuousBuilder 在引擎内部拼接，不依赖数据源直接提供。
    """
    datafeed = get_datafeed()
    database = get_database()

    symbols = list(cfg.PHYSICAL_SYMBOLS)

    print("=" * 60)
    print("📥 开始下载/同步历史数据...")
    for vt_symbol in symbols:
        s, e_str = vt_symbol.split(".")
        req = HistoryRequest(
            symbol=s,
            exchange=Exchange(e_str),
            start=cfg.START_DATE - timedelta(days=cfg.WARMUP_DAYS),
            end=cfg.END_DATE,
            interval=cfg.INTERVAL,
        )

        try:
            data = datafeed.query_bar_history(req)
            if data:
                database.save_bar_data(data)
                print(f"✅ 已同步: {vt_symbol} | {len(data)} 条")
            else:
                print(f"⚠️ 未获取到数据: {vt_symbol}")
        except Exception as e:
            print(f"❌ 下载失败: {vt_symbol} | {e}")
    print("📦 历史数据同步结束")
    print("=" * 60)


def run_main() -> None:
    # 1. 当前策略路由
    route = cfg.STRATEGY_ROUTES[cfg.CURRENT_STRATEGY]
    strategy_class_name = cfg.CURRENT_STRATEGY.value

    # 2. 自动下载物理合约数据
    if cfg.AUTO_DOWNLOAD:
        download_data(route)

    # 3. 动态导入策略
    module = importlib.import_module(route["module_path"])
    strategy_class = getattr(module, strategy_class_name)

    # 4. 初始化引擎
    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=route["vt_symbol"],
        interval=cfg.INTERVAL,
        start=cfg.START_DATE,
        end=cfg.END_DATE,
        rate=cfg.RATE,
        slippage=cfg.SLIPPAGE,
        size=cfg.SIZE,
        pricetick=cfg.PRICETICK,
        capital=cfg.CAPITAL,
        mode=cfg.MODE,
        physical_symbols=cfg.PHYSICAL_SYMBOLS,
        by_volume=cfg.BY_VOLUME,
        warmup_days=cfg.WARMUP_DAYS,
        friction_mode="v1.4",
    )

    # 5. 执行回测
    # engine.add_strategy(strategy_class, route["parameters"])
    engine.add_strategy(V13MockStrategy, {})
    engine.load_data()

    # 数据加载失败拦截
    if getattr(engine, "data_load_failed", False):
        error_msg = getattr(engine, 'data_load_error', '未知错误')
        print(f"❌ 数据加载失败: {error_msg}。回测已被中止！")
        sys.exit(1)

    engine.run_backtesting()

    # 运行时崩溃拦截
    if getattr(engine, "backtest_failed", False):
        error_msg = getattr(engine, 'backtest_error', '未知错误')
        print(f"❌ 回测执行过程中发生异常: {error_msg}。拒绝生成残缺报表！")
        sys.exit(1)

    # 6. 计算结果并生成报告
    df = engine.calculate_result()
    stats = engine.calculate_statistics()

    print("\n" + "=" * 50)
    print("⏳ 开始执行 V1.3 架构防爆与对账验证...")
    validate_v1_3_dod(engine)
    print("=" * 50 + "\n")

    print("\n⏳ 开始执行 V1.4 执行摩擦模块化验证...")
    validate_v1_4_directional_constraint(engine)
    validate_v1_4_double_deduction(engine)
    validate_v1_4_behavior_precision(engine)
    validate_v1_4_accounting_price(engine)
    print("=" * 50 + "\n")

    # 报告输出目录：相对于本脚本所在的 test/ 文件夹
    result_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result")
    os.makedirs(result_dir, exist_ok=True)

    generate_web_report(engine, df, stats, result_dir)


if __name__ == "__main__":
    run_main()
