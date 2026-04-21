"""
report_builder.py
─────────────────────────────────────────────────────────────────────────────
V1 回测报告渲染器（从 run_test.py 拆分）

职责：
  - 接收 BacktestingEngine 实例 + DataFrame + stats 字典
  - 构造全部 HTML 片段（QA 看板、图表 Tab、对账表、订单/成交表）
  - 写入 HTML 文件并在浏览器中打开

调用方：
  from report_builder import generate_web_report
  generate_web_report(engine, df, stats, result_dir)
─────────────────────────────────────────────────────────────────────────────
"""

import os
import webbrowser
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

# ──────────────────────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────────────────────


def _norm_dt(dt):
    """剥离 tzinfo，兼容 pd.Timestamp。"""
    if dt is None:
        return None
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _normalize_dt(dt):
    if dt is None:
        return pd.Timestamp.min
    ts = pd.Timestamp(dt)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    return ts


def _fmt_stat(key, val):
    """按指标类型格式化数值。"""
    PCT_KEYS = {"max_ddpercent", "total_return", "annual_return", "daily_return", "return_std"}
    RATIO_KEYS = {"sharpe_ratio", "ewm_sharpe", "return_drawdown_ratio", "rgr_ratio"}
    MONEY_KEYS = {
        "capital",
        "end_balance",
        "max_drawdown",
        "total_net_pnl",
        "daily_net_pnl",
        "total_commission",
        "daily_commission",
        "total_slippage",
        "daily_slippage",
        "total_turnover",
        "daily_turnover",
    }
    try:
        f = float(val)
        if key in PCT_KEYS: return f"{f:.4f} %"
        if key in RATIO_KEYS: return f"{f:.4f}"
        if key in MONEY_KEYS: return f"{f:,.2f}"
        if isinstance(val, float): return f"{f:.4f}"
        return str(val)
    except (TypeError, ValueError):
        return str(val)


# ──────────────────────────────────────────────────────────────────────────────
# 各 HTML 片段构建函数
# ──────────────────────────────────────────────────────────────────────────────


def _build_qa_summary(engine) -> str:
    """构建 QA 审计看板 HTML。"""
    from vnpy.trader.constant import Status as VnpyStatus

    on_init_blocked = sum(1 for o in getattr(engine, "warmup_blocked_orders", []) if o.get("phase") == "on_init")
    on_start_blocked = sum(1 for o in getattr(engine, "warmup_blocked_orders", []) if o.get("phase") == "on_start")
    mismatch_count = len(getattr(engine, "warmup_interval_mismatch_logs", []))

    audit_logs = getattr(engine, "order_audit_logs", {})
    rejected_orders = [o for o in engine.get_all_orders() if o.status == VnpyStatus.REJECTED]
    rejected_missing = [o.vt_orderid for o in rejected_orders if not audit_logs.get(o.vt_orderid, {}).get("status_reason")]
    cancelled_orders = [o for o in engine.get_all_orders() if o.status == VnpyStatus.CANCELLED]
    cancelled_missing = [o.vt_orderid for o in cancelled_orders if not audit_logs.get(o.vt_orderid, {}).get("status_reason")]

    norm_start = _norm_dt(engine.start) if hasattr(engine, "start") else None
    trades_before_start = sum(1 for t in engine.get_all_trades()
                              if t.datetime and norm_start and _norm_dt(t.datetime) < norm_start)

    rollover_failed = sum(1 for log in getattr(engine, "rollover_logs", []) if log.get("status") == "FAILED")
    skipped_rollover = len(getattr(engine, "rollover_skip_logs", []))

    raw_rollover_dts = [
        log.get("datetime") for log in getattr(engine, "rollover_logs", []) if log.get("status") not in ("FAILED", )
    ]
    clean_rollover_dts = {_norm_dt(d) for d in raw_rollover_dts if d is not None}
    missing_dts = []
    if clean_rollover_dts and hasattr(engine, "history_data"):
        dt_to_index = {_norm_dt(b.datetime): i for i, b in enumerate(engine.history_data)}
        for rdt in clean_rollover_dts:
            if dt_to_index.get(rdt) is None:
                missing_dts.append(rdt)

    dl_failed = getattr(engine, "data_load_failed", False)
    dl_status_html = (f"<span style='color:var(--red);font-weight:bold'>FAIL ({getattr(engine,'data_load_error','')})</span>"
                      if dl_failed else "<span style='color:var(--green);font-weight:bold'>PASS</span>")

    def _cell(val, pass_val=0, warn=False):
        ok = (val == pass_val)
        color = "var(--green)" if ok else ("var(--gold)" if warn else "var(--red)")
        return f"<td style='color:{color};font-weight:bold'>{val}</td>"

    rows = f"""
        <tr><td>基础数据加载 (Data Load)</td><td>{dl_status_html}</td><td>PASS 为通过</td></tr>
        <tr><td>初始化非法发单 (on_init Blocked)</td>{_cell(on_init_blocked)}<td>0 为 PASS</td></tr>
        <tr><td>启动期拦截 (on_start Blocked)</td>{_cell(on_start_blocked, warn=True)}<td>仅作警告参考</td></tr>
        <tr><td>回测开始前成交 (Trades Before Start)</td>{_cell(trades_before_start)}<td>0 为 PASS</td></tr>
        <tr><td>周期错配预警 (Interval Mismatch)</td>{_cell(mismatch_count, warn=True)}<td>大于0提示指标可能失真</td></tr>
        <tr><td>无原因拒单 (Reject Missing Reason)</td>{_cell(len(rejected_missing))}<td>0 为 PASS</td></tr>
        <tr><td>换月崩溃/失败 (Rollover Failed)</td>{_cell(rollover_failed)}<td>0 为 PASS</td></tr>
        <tr><td>换月缺失K线 (Missing Rollover DTs)</td>{_cell(len(missing_dts))}<td>0 为 PASS（节假日可能非零）</td></tr>
        <tr><td>非交易态跳过换月 (Skipped Rollovers)</td><td style='color:var(--muted)'>{skipped_rollover}</td><td>仅作参考（属正常机制）</td></tr>
        <tr><td>无原因撤单 (Cancel Missing Reason)</td>{_cell(len(cancelled_missing))}<td>0 为 PASS</td></tr>
    """

    return f"""
    <div class="card">
        <div class="card-header">🛡️ V1.2 引擎核心 QA 审计看板</div>
        <div class="card-body">
            <table class="data-table w-100 center-table">
                <thead><tr><th>检查项</th><th style="text-align:center">实际结果</th><th style="text-align:center">判定标准</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    """


def _build_chart_tabs(engine, df) -> str:
    if df is None or df.empty:
        return "<div class='card'>暂无图表数据</div>"

    plot_df = df.copy()
    capital = engine.capital

    if "balance" not in plot_df.columns:
        plot_df["balance"] = plot_df["net_pnl"].cumsum() + capital

    plot_df["net_value"] = plot_df["balance"] / capital
    plot_df["highlevel"] = plot_df["balance"].cummax()
    plot_df["ddpercent"] = (plot_df["balance"] - plot_df["highlevel"]) / plot_df["highlevel"] * 100
    plot_df["cum_pnl"] = plot_df["net_pnl"].cumsum()

    x = [str(i) for i in plot_df.index]
    net_value = plot_df["net_value"].astype(float).tolist()
    ddpercent = plot_df["ddpercent"].astype(float).tolist()
    net_pnl = plot_df["net_pnl"].astype(float).tolist()
    cum_pnl = plot_df["cum_pnl"].astype(float).tolist()

    # 稀疏采样 X 轴刻度，全程只显示约 12 个日期，消除密集重叠
    n_ticks = 12
    step = max(1, len(x) // n_ticks)
    sparse_tickvals = x[::step]
    sparse_ticktexts = sparse_tickvals

    fig = make_subplots(
        rows=4,
        cols=1,
        subplot_titles=["单位净值 (Net Value)", "回撤百分比 (Drawdown %)", "每日净盈亏 (Daily Pnl)", "累计净盈亏 (Cumulative Pnl)"],
        vertical_spacing=0.12)  # 加大子图间距，防止标题与日期重叠

    fig.add_trace(go.Scatter(x=x, y=net_value, mode="lines", name="单位净值", line=dict(color="#3b82f6")), row=1, col=1)

    fig.add_trace(go.Scatter(x=x,
                             y=ddpercent,
                             fill="tozeroy",
                             mode="lines",
                             name="回撤(%)",
                             line=dict(color="#f87171"),
                             fillcolor="rgba(248, 113, 113, 0.2)"),
                  row=2,
                  col=1)

    fig.add_trace(go.Bar(x=x, y=net_pnl, name="单日盈亏", marker_color="#34d399"), row=3, col=1)

    fig.add_trace(go.Scatter(x=x,
                             y=cum_pnl,
                             fill="tozeroy",
                             mode="lines",
                             name="累计盈亏",
                             line=dict(color="#22d3ee"),
                             fillcolor="rgba(34, 211, 238, 0.1)"),
                  row=4,
                  col=1)

    # 统一设置所有子图 X 轴为稀疏刻度 + 斜体排列
    fig.update_xaxes(
        type="category",
        tickmode="array",
        tickvals=sparse_tickvals,
        ticktext=sparse_ticktexts,
        tickangle=-35,
    )

    fig.update_layout(
        height=1100,
        template="plotly_dark",
        showlegend=False,
        margin=dict(l=50, r=50, t=40, b=60)  # ✅ 修复4：减小顶部 margin，底部留足斜体日期空间
    )

    chart_json = pio.to_json(fig, engine="json")

    # 用 <details> 实现原生折叠，默认展开，点击标题可收起
    return f"""
    <details class="card" open>
        <summary class="card-header" style="cursor:pointer; user-select:none; list-style:none; display:flex; align-items:center; gap:8px;">
            <span style="color:var(--accent); font-size:0.85rem;">▼</span>
            📈 核心指标可视化（点击收起/展开）
        </summary>
        <div class="card-body">
            <div id="main_chart" style="width:100%; height:1100px;"></div>
            <script>
                var chartData = {chart_json};
                Plotly.newPlot('main_chart', chartData.data, chartData.layout, {{responsive: true}});
                document.querySelector('details.card').addEventListener('toggle', function(e) {{
                    if (e.target.open) {{
                        setTimeout(function() {{ Plotly.relayout('main_chart', {{}}); }}, 50);
                    }}
                }});
            </script>
        </div>
    </details>"""


def _build_mapping_table(engine) -> str:
    """构建连续合约 vs 物理合约 K 线对账表。"""
    from vnpy.trader.constant import Interval as VnpyInterval

    offset_n = 2
    bar_route_map = getattr(engine, "bar_route_map", {})
    physical_bars_map = getattr(engine, "physical_bars", {})
    history_data_ref = getattr(engine, "history_data", [])
    rollover_logs = getattr(engine, "rollover_logs", [])
    rollover_skip_logs = getattr(engine, "rollover_skip_logs", [])

    raw_rollover_dts_2 = [
        log.get("datetime") for log in rollover_logs if log.get("datetime") and log.get("status") not in ("FAILED", )
    ]
    rollover_cost_dts = {_norm_dt(dt) for dt in raw_rollover_dts_2}

    route_change_dts_list = []
    prev_sym = None
    for bar in history_data_ref:
        dt = _norm_dt(bar.datetime)
        sym = bar_route_map.get(dt)
        if sym and prev_sym and sym != prev_sym:
            route_change_dts_list.append(dt)
        if sym:
            prev_sym = sym
    route_change_dts_set = set(route_change_dts_list)

    audit_dts = rollover_cost_dts | route_change_dts_set

    if not (bar_route_map and physical_bars_map and audit_dts and history_data_ref):
        return """
        <div class="card">
            <div class="card-header">📐 连续合约 vs 物理合约 K线对账</div>
            <div class="card-body"><div class="empty-state">未生成对账表：无换月事件，或映射/物理K线数据为空。</div></div>
        </div>"""

    try:
        is_daily = (engine.interval == VnpyInterval.DAILY)
    except Exception:
        is_daily = True
    offset_n = 2 if is_daily else 5

    dt_to_index_map = {_norm_dt(b.datetime): i for i, b in enumerate(history_data_ref)}
    target_indices = set()
    missing_mapping_dts = []

    for rdt in audit_dts:
        idx = dt_to_index_map.get(rdt)
        if idx is None:
            missing_mapping_dts.append(rdt)
            continue
        target_indices.update(range(max(0, idx - offset_n), min(len(history_data_ref), idx + offset_n + 1)))

    rows_html = []
    for idx in sorted(target_indices):
        bar = history_data_ref[idx]
        dt = _norm_dt(bar.datetime)
        mapped_sym = bar_route_map.get(dt, "MISSING")
        phys_bar = physical_bars_map.get((mapped_sym, dt))

        if phys_bar:
            po, ph, pl, pc = (f"{phys_bar.open_price:.2f}", f"{phys_bar.high_price:.2f}", f"{phys_bar.low_price:.2f}",
                              f"{phys_bar.close_price:.2f}")
            diff_val = bar.close_price - phys_bar.close_price
            close_diff = f"<span style='color:var(--muted)'>{diff_val:+.2f}</span>"
        else:
            po = ph = pl = pc = f"<span style='color:var(--red)'>N/A</span>"
            close_diff = f"<span style='color:var(--red)'>N/A</span>"

        is_cost = dt in rollover_cost_dts
        is_route = dt in route_change_dts_set
        if is_cost and is_route: flag, rc = "🔄 BOTH", " class='rollover-row'"
        elif is_cost: flag, rc = "💰 ROLLOVER", " class='rollover-row'"
        elif is_route: flag, rc = "🧭 ROUTE", " style='background:rgba(59,130,246,0.08)'"
        else: flag, rc = "", ""

        dt_str = dt.strftime("%Y-%m-%d %H:%M") if dt else "N/A"
        rows_html.append(f"""<tr{rc}>
            <td class='mono'>{dt_str}</td>
            <td class='mono' style='color:var(--accent2)'>{mapped_sym}</td>
            <td class='mono'>{bar.open_price:.2f}</td><td class='mono'>{bar.high_price:.2f}</td>
            <td class='mono'>{bar.low_price:.2f}</td><td class='mono'>{bar.close_price:.2f}</td>
            <td class='mono'>{po}</td><td class='mono'>{ph}</td>
            <td class='mono'>{pl}</td><td class='mono'>{pc}</td>
            <td class='mono'>{close_diff}</td>
            <td style='font-weight:bold'>{flag}</td>
        </tr>""")

    warn_html = ""
    if missing_mapping_dts:
        warn_html = f"<div class='hint-text' style='color:var(--gold)'>⚠️ 有 {len(missing_mapping_dts)} 个切换点无法在 history_data 中定位（可能为节假日）</div>"

    return f"""
    <div class="card">
        <div class="card-header">📐 连续合约 vs 物理合约 K线对账（切换点 ±{offset_n} 根）</div>
        <div class="card-body">
            {warn_html}
            <p class="hint-text">close_diff = 连续复权收盘 − 物理原始收盘，复权后差值非零属正常。
            🔄 BOTH = 映射切换且有换月摩擦；💰 ROLLOVER = 有换月摩擦；🧭 ROUTE = 仅映射切换。</p>
            <div class="scroll-box">
                <table class="data-table w-100">
                    <thead><tr>
                        <th>时间</th><th>映射物理合约</th>
                        <th>Cont O</th><th>Cont H</th><th>Cont L</th><th>Cont C</th>
                        <th>Phys O</th><th>Phys H</th><th>Phys L</th><th>Phys C</th>
                        <th>Close Diff</th><th>事件类型</th>
                    </tr></thead>
                    <tbody>{"".join(rows_html)}</tbody>
                </table>
            </div>
        </div>
    </div>"""


def _build_rollover_audit(engine) -> str:
    """构建引擎级换月事件审计表。"""
    rollover_logs = engine.get_rollover_logs()
    if not rollover_logs:
        return "<div class='alert-ok-box'>✅ 本次回测未检测到换月摩擦扣费。</div>"

    headers = "<th>换月时间</th><th>平旧合约</th><th>开新合约</th><th>方向</th><th>手数</th><th>结算基准价</th><th>双边手续费</th><th>双边滑点</th><th>摩擦总损耗</th>"
    rows = []
    for log in rollover_logs:
        dt_str = log["datetime"].strftime("%Y-%m-%d %H:%M")
        rows.append(f"""<tr class='rollover-row'>
            <td>{dt_str}</td>
            <td class='mono' style='color:var(--red)'>{log['old_symbol']}</td>
            <td class='mono' style='color:var(--green)'>{log['new_symbol']}</td>
            <td>{log['direction']}</td><td>{log['volume']}</td>
            <td class='mono'>{log['ref_price']:.2f}</td>
            <td class='mono'>{log['commission']:.2f}</td>
            <td class='mono'>{log['slippage']:.2f}</td>
            <td class='stat-val' style='color:var(--gold)'>{log['rollover_pnl']:.2f}</td>
        </tr>""")

    return f"""
    <p class="hint-text">🟡 数据来源：引擎底层换月事件账本 (Rollover Logs)</p>
    <div class="scroll-box">
        <table class="data-table w-100">
            <thead><tr>{headers}</tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>"""


def _build_stats_table(stats: dict) -> str:
    """构建绩效指标表格。"""
    rows = "".join(f"<tr><td class='stat-key'>{k}</td><td class='stat-val'>{_fmt_stat(k,v)}</td></tr>"
                   for k, v in stats.items())
    return f"""
    <table class="data-table w-100">
        <thead><tr><th>指标</th><th style="text-align:right">数值</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>"""


def _build_orders_table(engine) -> str:
    """构建订单生命周期表格。"""
    all_orders = []
    for o in engine.get_all_orders():
        sym = getattr(o, "vt_symbol", o.symbol)
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
        all_orders.append({
            "dt": so.datetime,
            "sym": so.vt_symbol,
            "type": "Stop",
            "dir": so.direction.value,
            "off": so.offset.value,
            "price": so.price,
            "vol": so.volume,
            "status": str(so.status.value),
            "reason": getattr(so, "cancel_reason", "")
        })

    all_orders.sort(key=lambda x: _normalize_dt(x["dt"]))

    if not all_orders:
        return "<div class='empty-state'>无委托记录</div>"

    rows = []
    for o in all_orders:
        is_cancelled = "撤销" in o["status"] or "CANCEL" in o["status"].upper()
        rc = " class='cancel-row'" if is_cancelled else ""
        badge = (f" <span class='reason-badge'>{o['reason']}</span>" if o["reason"] else "")
        dt_str = _normalize_dt(o["dt"]).strftime("%Y-%m-%d %H:%M")
        rows.append(f"<tr{rc}><td>{dt_str}</td><td class='mono'>{o['sym']}</td>"
                    f"<td>{o['type']}</td><td>{o['dir']}</td><td>{o['off']}</td>"
                    f"<td class='mono'>{o['price']:.2f}</td><td>{o['vol']}</td>"
                    f"<td>{o['status']}{badge}</td></tr>")

    return f"""<div class="scroll-box">
        <table class="data-table w-100">
            <thead><tr>
                <th>发单时间</th><th>合约</th><th>类型</th><th>方向</th>
                <th>动作</th><th>价格</th><th>数量</th><th>状态</th>
            </tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table></div>"""


def _build_trades_table(engine) -> str:
    """构建物理成交记录表格。"""
    trades = engine.get_all_trades()
    if not trades:
        return "<div class='empty-state'>无成交记录</div>"

    rows = []
    for t in trades:
        sym = getattr(t, "vt_symbol", f"{t.symbol}.{t.exchange.value}")
        rows.append(f"<tr><td>{pd.to_datetime(t.datetime).strftime('%Y-%m-%d %H:%M')}</td>"
                    f"<td class='mono'>{sym}</td><td>{t.direction.value}</td>"
                    f"<td>{t.offset.value}</td><td class='mono'>{t.price:.2f}</td>"
                    f"<td>{t.volume}</td></tr>")

    return f"""<div class="scroll-box">
        <table class="data-table w-100">
            <thead><tr>
                <th>成交时间</th><th>合约</th><th>方向</th>
                <th>动作</th><th>成交价</th><th>数量</th>
            </tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table></div>"""


def _build_daily_results_table(df) -> str:
    """构建日度盈亏明细表格"""
    if df is None or df.empty:
        return "<div class='empty-state'>无日度结算数据</div>"

    rows = []
    # 按照日期倒序排列，方便看最近的表现
    for dt, row in df.sort_index(ascending=False).iterrows():
        dt_str = dt.strftime("%Y-%m-%d")
        net_pnl_style = f"style='color: {'#34d399' if row['net_pnl'] >= 0 else '#f87171'}'"
        rows.append(f"<tr>"
                    f"<td>{dt_str}</td>"
                    f"<td class='mono'>{row['balance']:,.2f}</td>"
                    f"<td class='mono' {net_pnl_style}>{row['net_pnl']:,.2f}</td>"
                    f"<td class='mono'>{row['commission']:,.2f}</td>"
                    f"<td class='mono'>{row['slippage']:,.2f}</td>"
                    f"<td class='mono'>{row['trade_count']}</td>"
                    f"</tr>")

    return f"""
    <div class="card">
        <div class="card-header">📅 日度结算明细 (Daily Results)</div>
        <div class="card-body">
            <div class="scroll-box" style="max-height: 400px;">
                <table class="data-table w-100">
                    <thead><tr>
                        <th>日期</th><th>账户余额</th><th>当日净盈亏</th><th>手续费</th><th>滑点</th><th>成交数</th>
                    </tr></thead>
                    <tbody>{"".join(rows)}</tbody>
                </table>
            </div>
        </div>
    </div>"""


# ──────────────────────────────────────────────────────────────────────────────
# CSS + JS 模板
# ──────────────────────────────────────────────────────────────────────────────

_STYLE = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
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
}

body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    font-size: 0.875rem;
    line-height: 1.6;
    padding: 24px 20px 40px;
}

/* ─── 页面标题 ─── */
.page-header {
    display: flex; align-items: baseline; gap: 12px;
    margin-bottom: 24px; padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
}
.page-header h1 { font-size: 1.25rem; font-weight: 600; color: var(--text); }
.page-header .badge {
    font-family: var(--font-mono); font-size: 0.7rem;
    padding: 2px 8px; border-radius: 4px;
    background: var(--accent); color: white;
}

/* ─── 卡片 ─── */
.card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; overflow: hidden; margin-bottom: 16px;
}
.card-header {
    padding: 10px 16px; font-size: 0.8rem; font-weight: 600;
    letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--border); background: rgba(255,255,255,0.02);
}
.card-body { padding: 0; }

/* ─── 网格 ─── */
.grid-2   { display: grid; grid-template-columns: 1fr 1fr;  gap: 16px; }
.grid-8-4 { display: grid; grid-template-columns: 8fr 4fr;  gap: 16px; align-items: start; }
@media (max-width: 1100px) { .grid-8-4, .grid-2 { grid-template-columns: 1fr; } }

/* ─── 数据表格 ─── */
.scroll-box { max-height: 380px; overflow: auto; }
.data-table {
    width: 100%; border-collapse: collapse; font-size: 0.82rem;
    table-layout: auto;   /* 让各列自适应内容宽度 */
}
.data-table th {
    position: sticky; top: 0; z-index: 2;
    background: #1e2535; color: var(--muted);
    font-weight: 600; font-size: 0.75rem; letter-spacing: 0.05em;
    text-transform: uppercase; padding: 8px 12px;
    white-space: nowrap; text-align: left;
    border-bottom: 1px solid var(--border);
}
.data-table td {
    padding: 7px 12px; white-space: nowrap;
    border-bottom: 1px solid rgba(255,255,255,0.04); color: var(--text);
}
/* 对齐修复：center-table 让所有 th/td 居中 */
.center-table th, .center-table td { text-align: center !important; }
/* 但 center-table 第一列保持左对齐 */
.center-table th:first-child, .center-table td:first-child { text-align: left !important; }

.data-table tbody tr:hover td { background: rgba(59,130,246,0.07); }
.data-table .mono   { font-family: var(--font-mono); font-size: 0.8rem; }
.data-table .stat-key { color: var(--muted); font-size: 0.8rem; }
.data-table .stat-val { font-family: var(--font-mono); font-size: 0.82rem; text-align: right; color: var(--accent2); }
.w-100 { width: 100%; }

/* ─── 特殊行 ─── */
.rollover-row td { background: rgba(245,158,11,0.1) !important; color: var(--gold) !important; font-weight: 500; }
.cancel-row   td { color: var(--red) !important; }

/* ─── 提示文本 ─── */
.hint-text {
    font-size: 0.75rem; color: var(--muted);
    padding: 10px 14px 8px; border-bottom: 1px solid var(--border);
}

/* ─── 状态框 ─── */
.alert-danger-box, .alert-ok-box, .empty-state {
    padding: 24px; text-align: center; font-size: 0.85rem; color: var(--muted);
}
.alert-danger-box { color: var(--red); }
.alert-ok-box     { color: var(--green); }

/* ─── 撤单原因标签 ─── */
.reason-badge {
    font-size: 0.7rem; color: #856404;
    background: #fff3cd; padding: 2px 4px; border-radius: 3px;
}

/* ─── 图表 Tab ─── */
.chart-tabs { display: flex; }
.chart-tab {
    background: transparent; border: none; border-bottom: 2px solid transparent;
    color: var(--muted); font-size: 0.78rem; font-weight: 600;
    padding: 10px 18px; cursor: pointer; letter-spacing: 0.04em;
    transition: color .15s, border-color .15s;
}
.chart-tab:hover  { color: var(--text); }
.chart-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.chart-panel      { display: none; }
.chart-panel.active { display: block; }

/* ─── 折叠图表 ─── */
details.card > summary { outline: none; }
details.card > summary span { transition: transform .2s; display: inline-block; }
details.card:not([open]) > summary span { transform: rotate(-90deg); }
"""

_SCRIPT = """
function switchTab(id) {
    document.querySelectorAll('.chart-tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.chart-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-'   + id).classList.add('active');
    document.getElementById('panel-' + id).classList.add('active');
    // 触发 Plotly resize，防止首次渲染尺寸错误
    var panels = document.querySelectorAll('#panel-' + id + ' .js-plotly-plot');
    panels.forEach(function(el){ if(window.Plotly) Plotly.relayout(el, {}); });
}
"""

# ──────────────────────────────────────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────────────────────────────────────


def generate_web_report(engine, df, stats, result_dir):
    # 构建各个 HTML 片段
    qa_html = _build_qa_summary(engine)
    mapping_html = _build_mapping_table(engine)
    rollover_html = _build_rollover_audit(engine)
    chart_html = _build_chart_tabs(engine, df)
    daily_html = _build_daily_results_table(df)
    stats_html = _build_stats_table(stats)
    orders_html = _build_orders_table(engine)
    trades_html = _build_trades_table(engine)

    # 组装 HTML
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>V1 CTA 回测报告</title>
        <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
        <style>{_STYLE}</style>
    </head>
    <body>
        <div class="page-header">
            <h1>📊 V1 CTA 回测验真报告</h1>
            <span class="badge">PHASE 1</span>
        </div>

        {qa_html}
        {mapping_html}
        {chart_html}
        {daily_html}

        <div class="grid-8-4">
            <div>
                <div class="card">
                    <div class="card-header">引擎级换月事件审计 (Rollover Logs)</div>
                    <div class="card-body">{rollover_html}</div>
                </div>
                <div class="grid-2">
                    <div class="card">
                        <div class="card-header">订单生命周期</div>
                        <div class="card-body">{orders_html}</div>
                    </div>
                    <div class="card">
                        <div class="card-header">物理成交记录</div>
                        <div class="card-body">{trades_html}</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-header">绩效指标</div>
                <div class="card-body">
                    <div class="scroll-box" style="max-height:520px">{stats_html}</div>
                </div>
            </div>
        </div>

        <script>{_SCRIPT}</script>
    </body>
    </html>"""

    # 保存并打开报告
    report_file = os.path.join(result_dir, f"report_{datetime.now().strftime('%H%M%S')}.html")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html)
    webbrowser.open(f"file://{os.path.abspath(report_file)}")
