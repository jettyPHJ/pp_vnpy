import sys
import os

# ==============================================================
# 动态将项目根目录加入 Python 搜索路径
# ==============================================================
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# ==============================================================
import webbrowser
import pandas as pd
from datetime import datetime, timedelta
import config as cfg  # 引入配置文件

from vnpy.trader.object import HistoryRequest
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.database import get_database
from vnpy.trader.constant import Exchange

from vnpy_ctastrategy.backtesting import BacktestingEngine
from vnpy_ctastrategy.strategies.donchian_channel_strategy import DonchianChannelStrategy


def download_history_data():
    """根据配置文件自动同步物理合约历史数据"""
    print("\n" + "=" * 50)
    print("📡 启动自动数据同步 (Datafeed)...")
    print("=" * 50)
    datafeed = get_datafeed()
    database = get_database()
    if datafeed is None:
        print("❌ 未找到可用 Datafeed，跳过下载。")
        return

    # 前推 90 天以喂饱 ArrayManager 缓存
    req_start = cfg.START_DATE - timedelta(days=90)

    for vt_symbol in cfg.PHYSICAL_SYMBOLS:
        symbol, exchange_str = vt_symbol.split(".")
        req = HistoryRequest(symbol=symbol,
                             exchange=Exchange(exchange_str),
                             start=req_start,
                             end=cfg.END_DATE,
                             interval=cfg.INTERVAL)
        print(f"⏳ 请求 {vt_symbol} ...")
        data = datafeed.query_bar_history(req)
        if data:
            database.save_bar_data(data)
            print(f"✅ {vt_symbol} 成功入库: {len(data)} 条。")
    print("=" * 50 + "\n")


def generate_web_report(engine, df, stats):
    """渲染 HTML 报告并保存到 v1_result 目录"""
    print("⏳ 正在渲染交互式 Web 报告...")
    stats = stats or {}

    result_dir = os.path.join(current_dir, "v1_result")
    os.makedirs(result_dir, exist_ok=True)

    fig = engine.show_chart(df)
    chart_html = fig.to_html(full_html=False, include_plotlyjs='cdn') if fig else "<p>暂无图表</p>"
    stats_html = pd.Series(stats, name="数值").to_frame().to_html(classes="table table-sm table-striped table-hover",
                                                                header=False)

    # 换月 QA 审计表 (含上下文)
    qa_html = "<div class='alert alert-danger'>❌ 缺失 rollover_pnl 列，V1 扩展未生效！</div>"
    if df is not None and not df.empty and 'rollover_pnl' in df.columns:
        rollover_days = df[df['rollover_pnl'] < 0]
        if not rollover_days.empty:
            idx_positions = [df.index.get_loc(d) for d in rollover_days.index]
            context_idx = set()
            for i in idx_positions:
                if i > 0: context_idx.add(i - 1)
                context_idx.add(i)
                if i < len(df) - 1: context_idx.add(i + 1)

            qa_df = df.loc[df.index[list(context_idx)].sort_values()]
            display_cols = [c for c in ['close_price', 'end_pos', 'rollover_pnl', 'net_pnl'] if c in df.columns]

            def highlight_rollover(row):
                return ['background-color: #ffeeba'] * len(row) if row.name in rollover_days.index else [''] * len(row)

            qa_html = qa_df[display_cols].style.apply(highlight_rollover, axis=1).to_html(classes="table table-sm")
        else:
            qa_html = "<div class='alert alert-success'>✅ 本次回测未检测到任何换月摩擦扣费。</div>"

    # 订单生命周期与撤单追踪
    orders = engine.get_all_orders()
    if orders:
        orders_df = pd.DataFrame([{
            "发单时间": o.datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "合约": o.symbol,
            "方向": o.direction.value,
            "动作": o.offset.value,
            "价格": f"{o.price:.2f}",
            "数量": o.volume,
            "状态": o.status.value
        } for o in orders])
        orders_html = orders_df.style.apply(lambda row: ['color: red' if row['状态'] == '已撤销' else 'color: black'] * len(row),
                                            axis=1).to_html(classes="table table-sm table-striped text-center", index=False)
    else:
        orders_html = "<p>无委托记录。</p>"

    # 物理成交记录
    trades = engine.get_all_trades()
    trades_html = pd.DataFrame([{
        "成交时间": t.datetime.strftime("%Y-%m-%d %H:%M:%S"),
        "合约": t.symbol,
        "方向": t.direction.value,
        "动作": t.offset.value,
        "成交价": f"{t.price:.2f}",
        "数量": t.volume
    } for t in trades]).to_html(classes="table table-sm table-striped text-center", index=False) if trades else "<p>无成交记录。</p>"

    # 组装 HTML 模板
    html_template = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>V1 CTA 回测引擎 - 验真报告</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #f4f6f9; padding: 20px; font-size: 0.85rem; }}
            .card {{ box-shadow: 0 2px 4px rgba(0,0,0,0.05); margin-bottom: 20px; border: none; }}
            .card-header {{ background-color: #2c3e50; color: #fff; font-weight: bold; padding: 10px 15px; }}
            .scroll-box {{ max-height: 400px; overflow-y: auto; }}
            th {{ position: sticky; top: 0; background-color: #e9ecef !important; z-index: 1; }}
        </style>
    </head>
    <body>
        <div class="container-fluid">
            <h3 class="mb-3">🔬 V1 CTA 回测引擎 - 验真报告</h3>
            <div class="row">
                <div class="col-lg-8"><div class="card"><div class="card-header">📈 资金净值曲线</div><div class="card-body p-0">{chart_html}</div></div></div>
                <div class="col-lg-4">
                    <div class="card"><div class="card-header">🎯 换月 QA 审计 (T±1 上下文)</div><div class="card-body scroll-box">{qa_html}</div></div>
                    <div class="card"><div class="card-header">🏆 绩效指标</div><div class="card-body scroll-box p-0">{stats_html}</div></div>
                </div>
            </div>
            <div class="row">
                <div class="col-lg-6"><div class="card"><div class="card-header">🧾 订单生命周期 (含换月撤单追踪)</div><div class="card-body scroll-box p-0">{orders_html}</div></div></div>
                <div class="col-lg-6"><div class="card"><div class="card-header">🤝 物理成交明细</div><div class="card-body scroll-box p-0">{trades_html}</div></div></div>
            </div>
        </div>
    </body>
    </html>
    """

    report_path = os.path.join(result_dir, f"v1_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_template)

    print(f"✅ 报告已保存至: {report_path}")
    webbrowser.open(f"file://{report_path}")


def run_test():
    print("🚀 启动 V1 CTA 回测引擎...")

    if cfg.AUTO_DOWNLOAD:
        download_history_data()

    engine = BacktestingEngine()
    engine.set_parameters(vt_symbol=cfg.VT_SYMBOL,
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
                          by_volume=cfg.BY_VOLUME)

    engine.add_strategy(DonchianChannelStrategy, cfg.STRATEGY_SETTING)

    print("⚙️ 引擎正在进行历史回放与账本计算，请稍候...")
    engine.load_data()
    engine.run_backtesting()

    df = engine.calculate_result()
    stats = engine.calculate_statistics()

    generate_web_report(engine, df, stats)


if __name__ == "__main__":
    run_test()
