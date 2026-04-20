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
    """渲染交互式 Web 报告并自动在浏览器中打开"""
    print("⏳ 正在渲染交互式 Web 报告...")
    stats = stats or {}

    result_dir = os.path.join(current_dir, "v1_result")
    os.makedirs(result_dir, exist_ok=True)

    # ==========================================================
    # 1. 图表
    #    - 只设置 height，不动 legend/margin，防止覆盖 vnpy 4 子图布局
    #    - 用 update_layout 而非 to_html 的 default_width/height 参数（兼容性更好）
    # ==========================================================
    fig = engine.show_chart(df)
    if fig:
        fig.update_layout(
            height=780,
            autosize=True,
            template="plotly_white",
            hovermode="x unified",
        )
        chart_html = fig.to_html(
            full_html=False,
            include_plotlyjs="cdn",
            config={
                "responsive": True,
                "displaylogo": False
            },
        )
    else:
        chart_html = "<div class='empty-state'>暂无图表数据</div>"

    # ==========================================================
    # 2. 绩效指标
    #    - 浮点数格式化：百分比/金额/比率分类处理，避免长尾小数
    # ==========================================================
    PCT_KEYS = {"max_ddpercent", "total_return", "annual_return", "daily_return", "return_std"}
    RATIO_KEYS = {"sharpe_ratio", "ewm_sharpe", "return_drawdown_ratio", "rgr_ratio"}
    MONEY_KEYS = {
        "capital", "end_balance", "max_drawdown", "total_net_pnl", "daily_net_pnl", "total_commission", "daily_commission",
        "total_slippage", "daily_slippage", "total_turnover", "daily_turnover"
    }

    def fmt_stat(key, val):
        try:
            f = float(val)
            if key in PCT_KEYS:
                return f"{f:.4f} %"
            if key in RATIO_KEYS:
                return f"{f:.4f}"
            if key in MONEY_KEYS:
                return f"{f:,.2f}"
            if isinstance(val, float):
                return f"{f:.4f}"
            return str(val)
        except (TypeError, ValueError):
            return str(val)

    stats_rows = "".join(f"<tr><td class='stat-key'>{k}</td><td class='stat-val'>{fmt_stat(k, v)}</td></tr>"
                         for k, v in stats.items())
    stats_html = f"""
    <table class="data-table w-100">
        <thead><tr><th>指标</th><th>数值</th></tr></thead>
        <tbody>{stats_rows}</tbody>
    </table>
    """

    # ==========================================================
    # 3. 换月明细审计 (直接读取底层的 rollover_logs 账本)
    # ==========================================================
    rollover_logs = engine.get_rollover_logs()

    if not rollover_logs:
        qa_html = "<div class='alert-ok-box'>✅ 本次回测未检测到换月摩擦扣费。</div>"
    else:
        headers = "<th>换月时间</th><th>平旧合约</th><th>开新合约</th><th>方向</th><th>手数</th><th>结算基准价</th><th>双边手续费</th><th>双边滑点</th><th>摩擦总损耗</th>"
        rows = []
        for log in rollover_logs:
            dt_str = log['datetime'].strftime('%Y-%m-%d %H:%M')
            rows.append(f"<tr class='rollover-row'>"
                        f"<td>{dt_str}</td>"
                        f"<td class='mono' style='color:var(--red);'>{log['old_symbol']}</td>"
                        f"<td class='mono' style='color:var(--green);'>{log['new_symbol']}</td>"
                        f"<td>{log['direction']}</td>"
                        f"<td>{log['volume']}</td>"
                        f"<td class='mono'>{log['ref_price']:.2f}</td>"
                        f"<td class='mono'>{log['commission']:.2f}</td>"
                        f"<td class='mono'>{log['slippage']:.2f}</td>"
                        f"<td class='stat-val' style='color:var(--gold);'>{log['rollover_pnl']:.2f}</td>"
                        f"</tr>")

        qa_html = f"""
        <p class="hint-text">🟡 数据来源：引擎底层换月事件账本 (Rollover Logs)</p>
        <div class="scroll-box">
            <table class="data-table w-100">
                <thead><tr>{headers}</tr></thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
        """

    # ==========================================================
    # 4. 订单生命周期
    # ==========================================================
    def normalize_dt(dt):
        if dt is None: return pd.Timestamp.min
        ts = pd.Timestamp(dt)
        if ts.tzinfo is not None: ts = ts.tz_convert(None)
        return ts

    all_orders = []
    for o in engine.get_all_orders():
        sym = getattr(o, "vt_symbol", o.symbol)  # 统一使用完整 vt_symbol
        all_orders.append({
            "dt": o.datetime,
            "sym": sym,
            "type": "Limit",
            "dir": o.direction.value,
            "off": o.offset.value,
            "price": o.price,
            "vol": o.volume,
            "status": str(o.status.value),
            "reason": ""
        })

    for so in engine.get_all_stop_orders():
        reason = getattr(so, "cancel_reason", "")
        all_orders.append({
            "dt": so.datetime,
            "sym": so.vt_symbol,
            "type": "Stop",
            "dir": so.direction.value,
            "off": so.offset.value,
            "price": so.price,
            "vol": so.volume,
            "status": str(so.status.value),
            "reason": reason
        })

    all_orders.sort(key=lambda x: normalize_dt(x["dt"]))

    if all_orders:
        order_rows = []
        for o in all_orders:
            is_cancelled = "撤销" in o["status"] or "CANCEL" in o["status"].upper()
            row_class = " class='cancel-row'" if is_cancelled else ""
            reason_badge = f" <span style='font-size:0.7rem;color:#856404;background:#fff3cd;padding:2px 4px;border-radius:3px;'>{o['reason']}</span>" if o[
                'reason'] else ""
            order_rows.append(
                f"<tr{row_class}><td>{normalize_dt(o['dt']).strftime('%Y-%m-%d %H:%M')}</td><td class='mono'>{o['sym']}</td><td>{o['type']}</td><td>{o['dir']}</td><td>{o['off']}</td><td class='mono'>{o['price']:.2f}</td><td>{o['vol']}</td><td>{o['status']}{reason_badge}</td></tr>"
            )
        orders_html = f"""<div class="scroll-box"><table class="data-table w-100"><thead><tr><th>发单时间</th><th>合约</th><th>类型</th><th>方向</th><th>动作</th><th>价格</th><th>数量</th><th>状态</th></tr></thead><tbody>{''.join(order_rows)}</tbody></table></div>"""
    else:
        orders_html = "<div class='empty-state'>无委托记录</div>"

    # ==========================================================
    # 5. 成交记录 (统一显示完整 vt_symbol)
    # ==========================================================
    trades = engine.get_all_trades()
    if trades:
        trade_rows = []
        for t in trades:
            sym = getattr(t, "vt_symbol", f"{t.symbol}.{t.exchange.value}")  # 👈 统一完整符号
            trade_rows.append(f"<tr>"
                              f"<td>{pd.to_datetime(t.datetime).strftime('%Y-%m-%d %H:%M')}</td>"
                              f"<td class='mono'>{sym}</td>"
                              f"<td>{t.direction.value}</td>"
                              f"<td>{t.offset.value}</td>"
                              f"<td class='mono'>{t.price:.2f}</td>"
                              f"<td>{t.volume}</td>"
                              f"</tr>")
        trades_html = f"""
        <div class="scroll-box">
            <table class="data-table w-100">
                <thead><tr>
                    <th>成交时间</th><th>合约</th><th>方向</th>
                    <th>动作</th><th>成交价</th><th>数量</th>
                </tr></thead>
                <tbody>{''.join(trade_rows)}</tbody>
            </table>
        </div>
        """
    else:
        trades_html = "<div class='empty-state'>无成交记录</div>"

    # ==========================================================
    # 6. HTML 模板
    # ==========================================================
    html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>V1 CTA 回测报告</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans+SC:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
 
        :root {{
            --bg:        #0f1117;
            --surface:   #181c27;
            --border:    #272d3d;
            --accent:    #3b82f6;
            --accent2:   #22d3ee;
            --gold:      #f59e0b;
            --red:       #f87171;
            --green:     #34d399;
            --text:      #e2e8f0;
            --muted:     #64748b;
            --font-body: 'IBM Plex Sans SC', sans-serif;
            --font-mono: 'IBM Plex Mono', monospace;
        }}
 
        body {{
            background: var(--bg);
            color: var(--text);
            font-family: var(--font-body);
            font-size: 0.875rem;
            line-height: 1.6;
            padding: 24px 20px 40px;
        }}
 
        /* ─── 页面标题 ─── */
        .page-header {{
            display: flex;
            align-items: baseline;
            gap: 12px;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }}
        .page-header h1 {{
            font-size: 1.25rem;
            font-weight: 600;
            letter-spacing: 0.01em;
            color: var(--text);
        }}
        .page-header .badge {{
            font-family: var(--font-mono);
            font-size: 0.7rem;
            padding: 2px 8px;
            border-radius: 4px;
            background: var(--accent);
            color: white;
            letter-spacing: 0.05em;
        }}
 
        /* ─── 卡片 ─── */
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
            overflow: hidden;
            margin-bottom: 16px;
        }}
        .card-header {{
            padding: 10px 16px;
            font-size: 0.8rem;
            font-weight: 600;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--muted);
            border-bottom: 1px solid var(--border);
            background: rgba(255,255,255,0.02);
        }}
        .card-body {{ padding: 0; }}
        .card-body.padded {{ padding: 14px 16px; }}
 
        /* ─── 网格 ─── */
        .grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }}
        .grid-8-4 {{
            display: grid;
            grid-template-columns: 8fr 4fr;
            gap: 16px;
            align-items: start;
        }}
        @media (max-width: 1100px) {{
            .grid-8-4, .grid-2 {{ grid-template-columns: 1fr; }}
        }}
 
        /* ─── 数据表格 ─── */
        .scroll-box {{
            max-height: 380px;
            overflow: auto;
        }}
        .data-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.82rem;
        }}
        .data-table th {{
            position: sticky;
            top: 0;
            z-index: 2;
            background: #1e2535;
            color: var(--muted);
            font-weight: 600;
            font-size: 0.75rem;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            padding: 8px 12px;
            white-space: nowrap;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }}
        .data-table td {{
            padding: 7px 12px;
            white-space: nowrap;
            border-bottom: 1px solid rgba(255,255,255,0.04);
            color: var(--text);
        }}
        .data-table tbody tr:hover td {{
            background: rgba(59,130,246,0.07);
        }}
        .data-table .mono {{
            font-family: var(--font-mono);
            font-size: 0.8rem;
        }}
        .data-table .stat-key {{
            color: var(--muted);
            font-size: 0.8rem;
        }}
        .data-table .stat-val {{
            font-family: var(--font-mono);
            font-size: 0.82rem;
            text-align: right;
            color: var(--accent2);
        }}
        .w-100 {{ width: 100%; }}
 
        /* ─── 特殊行 ─── */
        .rollover-row td {{
            background: rgba(245,158,11,0.1) !important;
            color: var(--gold) !important;
            font-weight: 500;
        }}
        .cancel-row td {{
            color: var(--red) !important;
        }}
 
        /* ─── 提示文本 ─── */
        .hint-text {{
            font-size: 0.75rem;
            color: var(--muted);
            padding: 10px 14px 8px;
            border-bottom: 1px solid var(--border);
        }}
 
        /* ─── 状态框 ─── */
        .alert-danger-box, .alert-ok-box, .empty-state {{
            padding: 24px;
            text-align: center;
            font-size: 0.85rem;
            color: var(--muted);
        }}
        .alert-danger-box {{ color: var(--red); }}
        .alert-ok-box {{ color: var(--green); }}
 
        /* ─── Plotly 图表容器 ─── */
        .chart-container {{
            min-height: 780px;
            background: white;
        }}
        .chart-container > div {{ width: 100% !important; }}
    </style>
</head>
<body>
    <div class="page-header">
        <h1>📊 V1 CTA 回测验真报告</h1>
        <span class="badge">PHASE 1</span>
    </div>
 
    <div class="grid-8-4">
        <!-- 左列：图表 -->
        <div>
            <div class="card">
                <div class="card-header">资金净值曲线</div>
                <div class="card-body chart-container">
                    {chart_html}
                </div>
            </div>
        </div>
 
        <!-- 右列：换月审计 + 绩效指标 -->
        <div>
            <div class="card">
                <div class="card-header">引擎级换月事件审计 (Rollover Logs)</div>
                <div class="card-body">
                    {qa_html}
                </div>
            </div>
 
            <div class="card">
                <div class="card-header">绩效指标</div>
                <div class="card-body">
                    <p class="hint-text" style="color: #f59e0b; font-weight: 500; text-align: center; border-bottom: 1px solid var(--border); padding-bottom: 10px;">
                        ⚠️ 已知局限：若策略加载未拼接的虚拟历史，前 N 根 K 线将作为暖机期无交易信号。
                    </p>
                    <div class="scroll-box" style="max-height:340px">
                        {stats_html}
                    </div>
                </div>
            </div>
        </div>
    </div>
 
    <div class="grid-2">
        <div class="card">
            <div class="card-header">订单生命周期</div>
            <div class="card-body">
                {orders_html}
            </div>
        </div>
 
        <div class="card">
            <div class="card-header">物理成交记录</div>
            <div class="card-body">
                {trades_html}
            </div>
        </div>
    </div>
</body>
</html>"""

    report_path = os.path.join(result_dir, f"v1_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_template)

    print(f"✅ 报告已保存: {report_path}")
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
