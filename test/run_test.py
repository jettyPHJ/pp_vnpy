import sys
import os
import importlib
from datetime import timedelta

# 保证项目根目录在 sys.path 中
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import config as cfg
from vnpy_ctastrategy.backtesting import BacktestingEngine, load_tick_data
from report_builder import generate_web_report
from system_validator import run_all_system_validations

from vnpy.trader.object import HistoryRequest
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange
from vnpy_ctastrategy.strategies.v13_mock_strategy import V13MockStrategy
from vnpy_ctastrategy.base import ExecutionProfile


def _split_vt_symbol(vt_symbol: str) -> tuple[str, Exchange]:
    """拆分 vt_symbol，返回 symbol 与 Exchange 枚举。"""
    symbol, exchange_str = vt_symbol.split(".")
    return symbol, Exchange(exchange_str)


def print_v16_symbol_topology(signal_vt_symbol: str) -> None:
    """打印 V1.6 的信号层/执行层分离关系，避免误把 rb888 当执行合约。"""
    print("=" * 72)
    print("🧭 V1.6 测试拓扑")
    print(f"📈 信号标的 / 连续合约: {signal_vt_symbol}")
    print("🧱 物理执行合约池:")
    for vt_sym in cfg.PHYSICAL_SYMBOLS:
        print(f"   - {vt_sym}")
    print("说明：策略可以继续读取 rb888.SHFE 的连续 Bar 信号；")
    print("      Tick Replay 执行层必须使用上面的物理合约 Tick。")
    print("=" * 72)


def inspect_local_tick_data() -> int:
    """检查本地数据库中物理合约 Tick 数量；不下载，只检查。"""
    total_ticks = 0
    print("\n" + "=" * 72)
    print("🔎 检查本地物理合约 Tick 数据...")
    for vt_symbol in cfg.PHYSICAL_SYMBOLS:
        try:
            symbol, exchange = _split_vt_symbol(vt_symbol)
            ticks = load_tick_data(
                symbol,
                exchange,
                cfg.START_DATE - timedelta(days=cfg.WARMUP_DAYS),
                cfg.END_DATE,
            )
            count = len(ticks or [])
            total_ticks += count
            icon = "✅" if count > 0 else "⚠️"
            print(f"{icon} {vt_symbol}: {count} ticks")
        except Exception as exc:
            print(f"❌ {vt_symbol}: Tick 检查失败 | {exc}")
    print(f"📊 本地物理 Tick 总数: {total_ticks}")
    if total_ticks <= 0:
        print("⚠️ 未发现物理合约 Tick。V1.6 runtime validator 会失败，这是正确的保护。")
        print("   你需要先把 rb2405/rb2410/... 等物理合约 Tick 写入 vn.py 数据库。")
    print("=" * 72 + "\n")
    return total_ticks


def download_data(route: dict = None) -> None:
    """根据配置同步历史 Bar 数据。

    注意：这里同步的是物理合约 Bar 数据，用于连续合约构造与路由。
    V1.6 Tick Replay 还需要物理合约 Tick 数据；常规 datafeed 未必支持 tick 下载，
    因此这里不假设可以自动下载 Tick，只在回测前用 inspect_local_tick_data() 检查本地库。
    """
    datafeed = get_datafeed()
    database = get_database()

    symbols = list(cfg.PHYSICAL_SYMBOLS)

    print("=" * 72)
    print("📥 开始下载/同步物理合约 Bar 数据...")
    for vt_symbol in symbols:
        s, e = _split_vt_symbol(vt_symbol)
        req = HistoryRequest(
            symbol=s,
            exchange=e,
            start=cfg.START_DATE - timedelta(days=cfg.WARMUP_DAYS),
            end=cfg.END_DATE,
            interval=cfg.INTERVAL,
        )

        try:
            data = datafeed.query_bar_history(req)
            if data:
                database.save_bar_data(data)
                print(f"✅ 已同步 Bar: {vt_symbol} | {len(data)} 条")
            else:
                print(f"⚠️ 未获取到 Bar: {vt_symbol}")
        except Exception as exc:
            print(f"❌ Bar 下载失败: {vt_symbol} | {exc}")
    print("📦 物理合约 Bar 数据同步结束")
    print("=" * 72)


def run_backtest_with_config(strategy_class, setting: dict, vt_symbol: str):
    engine = BacktestingEngine()

    # 🚨 修复点：vt_symbol 改为由参数传入，不再从全局 cfg 获取
    engine.set_parameters(vt_symbol=vt_symbol,
                          interval=cfg.INTERVAL,
                          start=cfg.START_DATE,
                          end=cfg.END_DATE,
                          rate=cfg.RATE,
                          slippage=cfg.SLIPPAGE,
                          size=cfg.SIZE,
                          pricetick=cfg.PRICETICK,
                          capital=cfg.CAPITAL,
                          physical_symbols=cfg.PHYSICAL_SYMBOLS,
                          warmup_days=cfg.WARMUP_DAYS)

    # 2. 配置物理仿真执行环境
    # V1.6：必须在 load_data() 前启用 Tick Replay，否则物理 Tick 不会被加载进 TickReplayStore。
    engine.configure_execution(profile=ExecutionProfile.REALISTIC,
                               margin_rate=0.10,
                               max_order_size=50.0,
                               max_participation_rate=0.15,
                               impact_factor=1.5,
                               use_tick_replay=True)

    engine.add_strategy(strategy_class, setting)
    engine.load_data()

    stats = getattr(engine, "tick_replay_stats", {})
    if stats:
        print("📊 TickReplayStore 加载统计:", stats)

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

    # ── 架构级系统验证（V1.3 ~ V1.6 全量防回退测试）────────────────────────
    # 必须在 calculate_result() 之前执行，此时引擎交易数据最完整。
    # 传入 engine 实例，V1.3/V1.4 验证器才能拿到真实的 trades / chain_audit_archive。
    print("\n" + "─" * 60)
    print("🔍 回测完成，开始执行架构级系统验证...")
    print("─" * 60)
    try:
        run_all_system_validations(engine)
    except AssertionError as e:
        print(f"\n🚨 系统验证失败！请立即修复后再生成报表：\n   {e}")
        sys.exit(2)
    except Exception as e:
        print(f"\n⚠️ 系统验证发生意外异常（非断言）：{e}")
        # 非预期异常打印后继续生成报表，方便调试
    print("─" * 60 + "\n")

    # 6. 计算结果并生成报告
    df = engine.calculate_result()
    stats = engine.calculate_statistics()

    # 报告输出目录：相对于本脚本所在的 test/ 文件夹
    result_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result")
    os.makedirs(result_dir, exist_ok=True)

    generate_web_report(engine, df, stats, result_dir)


if __name__ == "__main__":
    # 1. 提取当前策略配置枚举
    current_strategy_enum = getattr(cfg, "CURRENT_STRATEGY", None)

    # 2. 动态路由解析策略类、配置参数和标的合约
    if current_strategy_enum and hasattr(cfg, "STRATEGY_ROUTES") and current_strategy_enum in cfg.STRATEGY_ROUTES:
        route = cfg.STRATEGY_ROUTES[current_strategy_enum]

        # 动态导入模块与类
        module = importlib.import_module(route["module_path"])
        strategy_class = getattr(module, current_strategy_enum.value)

        # 提取参数
        setting = route.get("parameters", {})
        target_vt_symbol = route.get("vt_symbol", "rb888.SHFE")
    else:
        # 降级兜底方案
        strategy_class = V13MockStrategy
        setting = {}
        target_vt_symbol = "rb888.SHFE"

    print(f"▶️ 当前执行策略: {strategy_class.__name__}")
    print_v16_symbol_topology(target_vt_symbol)

    if getattr(cfg, "AUTO_DOWNLOAD", False):
        download_data(route if 'route' in locals() else None)

    inspect_local_tick_data()

    # 3. 传入正确的类、参数和标的启动回测
    run_backtest_with_config(strategy_class, setting, target_vt_symbol)
