"""
run_test.py
─────────────────────────────────────────────────────────────────────────────
V1 回测基线引擎 — 主入口

职责：
  1. 按 config.py 配置自动下载历史数据（可关闭）
  2. 初始化并运行 BacktestingEngine
  3. 调用 report_builder.generate_web_report() 渲染 HTML 报告

报告渲染逻辑已完整拆分至 report_builder.py，本文件保持精简。
─────────────────────────────────────────────────────────────────────────────
"""

import sys
import os

# ── 将项目根目录加入搜索路径 ───────────────────────────────────────────────────
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
# ─────────────────────────────────────────────────────────────────────────────

from datetime import timedelta

import config as cfg  # noqa: E402  (在 sys.path 修改后引入)

from vnpy.trader.object import HistoryRequest
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange

from vnpy_ctastrategy.backtesting import BacktestingEngine
from vnpy_ctastrategy.strategies.donchian_channel_strategy import DonchianChannelStrategy

from report_builder import generate_web_report

# ──────────────────────────────────────────────────────────────────────────────
# 数据下载
# ──────────────────────────────────────────────────────────────────────────────


def download_history_data() -> None:
    """根据 config.py 自动同步物理合约历史数据。"""
    print("\n" + "=" * 50)
    print("📡 启动自动数据同步 (Datafeed)...")
    print("=" * 50)

    datafeed = get_datafeed()
    database = get_database()

    if datafeed is None:
        print("❌ 未找到可用 Datafeed，跳过下载。")
        return

    req_start = cfg.START_DATE - timedelta(days=90)

    for vt_symbol in cfg.PHYSICAL_SYMBOLS:
        symbol, exchange_str = vt_symbol.split(".")
        req = HistoryRequest(
            symbol=symbol,
            exchange=Exchange(exchange_str),
            start=req_start,
            end=cfg.END_DATE,
            interval=cfg.INTERVAL,
        )
        print(f"⏳ 请求 {vt_symbol} ...")
        data = datafeed.query_bar_history(req)
        if data:
            database.save_bar_data(data)
            print(f"✅ {vt_symbol} 成功入库: {len(data)} 条。")

    print("=" * 50 + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────────────


def run_test() -> None:
    print("🚀 启动 V1 CTA 回测引擎...")

    # 1. 可选：自动下载数据
    if cfg.AUTO_DOWNLOAD:
        download_history_data()

    # 2. 初始化引擎
    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=cfg.VT_SYMBOL,
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
        warmup_days=getattr(cfg, "WARMUP_DAYS", 120),
    )
    engine.add_strategy(DonchianChannelStrategy, cfg.STRATEGY_SETTING)

    # 3. 回测
    print("⚙️  引擎正在进行历史回放与账本计算，请稍候...")
    engine.load_data()
    engine.run_backtesting()

    # 4. 统计
    df = engine.calculate_result()
    stats = engine.calculate_statistics()

    # ==========================================
    # V1.2 会计级强断言 QA 网
    # ==========================================
    print("🔎 正在执行双轨制坐标系一致性断言...")
    pricetick = getattr(cfg, "PRICETICK", 1.0)

    for t in engine.get_all_trades():
        assert hasattr(t, "physical_price"), f"成交单缺失物理价"
        assert hasattr(t, "accounting_price"), f"成交单缺失会计价"
        # 存在性与闭合校验
        assert abs(t.accounting_price - t.physical_price - getattr(t, "price_offset", 0.0)) < 1e-6, "成交价坐标公式不成立"

    for o in engine.get_all_orders():
        phys = getattr(o, "physical_price", getattr(o, "price", None))
        acct = getattr(o, "accounting_price", phys)
        off = getattr(o, "price_offset", 0.0)
        assert phys is not None, f"订单缺失物理价"
        assert abs(acct - phys - off) <= max(pricetick, 1e-6), "限价单坐标公式不成立"

    for so in engine.get_all_stop_orders():
        phys = getattr(so, "physical_price", getattr(so, "price", None))
        acct = getattr(so, "accounting_price", phys)
        off = getattr(so, "price_offset", 0.0)
        assert phys is not None, f"止损单缺失物理价"
        assert abs(acct - phys - off) <= max(pricetick, 1e-6), "止损单坐标公式不成立"

    for d, r in engine.daily_results.items():
        assert r.close_price > 0, f"{d} 日结算价异常污染"

    print("✅ V1.2 会计断言全部通过！系统净值逻辑已彻底闭环。")
    # ==========================================

    # 5. 渲染报告
    result_dir = os.path.join(current_dir, "v1_result")
    generate_web_report(engine, df, stats, result_dir)


if __name__ == "__main__":
    run_test()
