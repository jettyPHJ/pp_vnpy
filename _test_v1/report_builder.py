"""
report_builder.py
─────────────────────────────────────────────────────────────────────────────
V1 回测报告渲染器

职责：
  - 接收 BacktestingEngine 实例 + DataFrame + stats 字典
  - 构造全部 HTML 片段（QA 看板、图表、对账表、订单/成交表）
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
        "capital", "end_balance", "max_drawdown", "total_net_pnl", "daily_net_pnl", "total_commission", "daily_commission",
        "total_slippage", "daily_slippage", "total_turnover", "daily_turnover", "total_rollover_commission",
        "total_rollover_slippage", "all_in_commission", "all_in_slippage"
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
# HTML 片段构建
# ──────────────────────────────────────────────────────────────────────────────


def _build_qa_summary(engine) -> str:
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

    def _badge(val, pass_val=0, warn=False):
        ok = (val == pass_val)
        if ok:
            return f"<span class='qa-badge qa-pass'>{val}</span>"
        elif warn:
            return f"<span class='qa-badge qa-warn'>{val}</span>"
        else:
            return f"<span class='qa-badge qa-fail'>{val}</span>"

    dl_badge = ("<span class='qa-badge qa-fail'>FAIL</span>" if dl_failed else "<span class='qa-badge qa-pass'>PASS</span>")

    items = [
        ("基础数据加载", "Data Load", dl_badge, "PASS 为通过"),
        ("初始化非法发单", "on_init Blocked", _badge(on_init_blocked), "0 为 PASS"),
        ("启动期拦截", "on_start Blocked", _badge(on_start_blocked, warn=True), "仅作参考"),
        ("回测前成交", "Trades Before Start", _badge(trades_before_start), "0 为 PASS"),
        ("周期错配预警", "Interval Mismatch", _badge(mismatch_count, warn=True), "大于0提示失真"),
        ("无原因拒单", "Reject Missing Reason", _badge(len(rejected_missing)), "0 为 PASS"),
        ("换月崩溃", "Rollover Failed", _badge(rollover_failed), "0 为 PASS"),
        ("换月缺失K线", "Missing Rollover DTs", _badge(len(missing_dts)), "0 为 PASS"),
        ("非交易态跳过换月", "Skipped Rollovers", f"<span class='qa-badge qa-muted'>{skipped_rollover}</span>", "仅作参考"),
        ("无原因撤单", "Cancel Missing Reason", _badge(len(cancelled_missing)), "0 为 PASS"),
    ]

    rows_html = ""
    for zh, en, badge, criterion in items:
        rows_html += f"""
        <tr>
            <td><span class='qa-label-zh'>{zh}</span><span class='qa-label-en'>{en}</span></td>
            <td style='text-align:center'>{badge}</td>
            <td class='qa-criterion'>{criterion}</td>
        </tr>"""

    return f"""
    <div class="section-card">
        <div class="section-header">
            <span class="section-icon">🛡</span>
            <span>引擎 QA 审计</span>
            <span class="section-sub">Engine Core QA</span>
        </div>
        <table class="base-table qa-table">
            <thead><tr><th>检查项</th><th>结果</th><th>标准</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
    </div>"""


def _build_kpi_strip(stats: dict) -> str:
    """顶部 KPI 横条：最重要的 5 个指标一目了然。"""

    def _kpi(label, key, positive_is_good=True):
        raw = stats.get(key)
        val = _fmt_stat(key, raw) if raw is not None else "—"
        try:
            f = float(raw)
            if positive_is_good:
                color_cls = "kpi-pos" if f >= 0 else "kpi-neg"
            else:
                color_cls = "kpi-neg" if f < 0 else "kpi-pos"
        except (TypeError, ValueError):
            color_cls = ""
        return f"""
        <div class="kpi-cell">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value {color_cls}">{val}</div>
        </div>"""

    return f"""
    <div class="kpi-strip">
        {_kpi("总收益率", "total_return")}
        {_kpi("年化收益率", "annual_return")}
        {_kpi("最大回撤", "max_ddpercent", positive_is_good=False)}
        {_kpi("夏普比率", "sharpe_ratio")}
        {_kpi("期末资金", "end_balance")}
    </div>"""


def _build_stats_panel(stats: dict) -> str:
    """右侧绩效详情面板，分组展示。"""
    GROUPS = [
        ("盈亏表现", [
            ("总收益率", "total_return"),
            ("年化收益率", "annual_return"),
            ("总净盈亏", "total_net_pnl"),
            ("日均盈亏", "daily_net_pnl"),
            ("期末资金", "end_balance"),
        ]),
        ("风险指标", [
            ("最大回撤", "max_drawdown"),
            ("回撤百分比", "max_ddpercent"),
            ("最大回撤期(交易日)", "max_drawdown_duration"),
            ("夏普比率", "sharpe_ratio"),
            ("EWM Sharpe", "ewm_sharpe"),
            ("收益回撤比", "return_drawdown_ratio"),
            ("RGR Ratio", "rgr_ratio"),
        ]),
        ("成本摩擦 All-in", [
            ("策略成交笔数", "total_trade_count"),
            ("换月次数", "rollover_count"),
            ("策略手续费", "total_commission"),
            ("换月手续费", "total_rollover_commission"),
            ("综合手续费", "all_in_commission"),
            ("策略滑点", "total_slippage"),
            ("换月滑点", "total_rollover_slippage"),
            ("综合滑点", "all_in_slippage"),
        ]),
        ("统计周期", [
            ("开始日期", "start_date"),
            ("结束日期", "end_date"),
            ("总交易日", "total_days"),
            ("起始资金", "capital"),
        ]),
    ]

    HIGHLIGHT = {"all_in_commission", "all_in_slippage", "rollover_count"}

    html = []
    for group_name, keys in GROUPS:
        html.append(f"<div class='stat-group-label'>{group_name}</div>")
        for label, k in keys:
            if k not in stats:
                continue
            val = _fmt_stat(k, stats[k])
            hl = " stat-highlight" if k in HIGHLIGHT else ""
            html.append(f"""
            <div class="stat-row{hl}">
                <span class="stat-key">{label}</span>
                <span class="stat-val">{val}</span>
            </div>""")

    return "\n".join(html)


def _build_chart(engine, df) -> str:
    if df is None or df.empty:
        return "<div class='empty-state'>暂无图表数据</div>"

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

    step = max(1, len(x) // 10)
    sparse_x = x[::step]

    fig = make_subplots(
        rows=4,
        cols=1,
        subplot_titles=["单位净值", "回撤 %", "每日盈亏", "累计盈亏"],
        vertical_spacing=0.10,
    )
    fig.add_trace(go.Scatter(x=x, y=net_value, mode="lines", name="净值", line=dict(color="#3b82f6", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x,
                             y=ddpercent,
                             fill="tozeroy",
                             name="回撤%",
                             line=dict(color="#f87171", width=1),
                             fillcolor="rgba(248,113,113,0.15)"),
                  row=2,
                  col=1)
    fig.add_trace(go.Bar(x=x, y=net_pnl, name="日盈亏", marker_color="rgba(52,211,153,0.7)"), row=3, col=1)
    fig.add_trace(go.Scatter(x=x,
                             y=cum_pnl,
                             fill="tozeroy",
                             name="累计盈亏",
                             line=dict(color="#22d3ee", width=1.5),
                             fillcolor="rgba(34,211,238,0.08)"),
                  row=4,
                  col=1)

    fig.update_xaxes(type="category", tickmode="array", tickvals=sparse_x, ticktext=sparse_x, tickangle=-30)
    fig.update_layout(
        height=1000,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        margin=dict(l=55, r=20, t=36, b=50),
        font=dict(family="'IBM Plex Mono', monospace", size=11, color="#94a3b8"),
    )
    for i in range(1, 5):
        fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.05)", row=i, col=1)
        fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.05)", row=i, col=1)

    chart_json = pio.to_json(fig, engine="json")
    return f"""
    <div id="main_chart" style="width:100%;height:1000px;"></div>
    <script>
        (function(){{
            var d = {chart_json};
            Plotly.newPlot('main_chart', d.data, d.layout, {{responsive:true, displayModeBar:false}});
        }})();
    </script>"""


def _build_mapping_table(engine) -> str:
    from vnpy.trader.constant import Interval as VnpyInterval

    bar_route_map = getattr(engine, "bar_route_map", {})
    physical_bars_map = getattr(engine, "physical_bars", {})
    history_data_ref = getattr(engine, "history_data", [])
    rollover_logs = getattr(engine, "rollover_logs", [])

    raw_rollover_dts = [
        log.get("datetime") for log in rollover_logs if log.get("datetime") and log.get("status") not in ("FAILED", )
    ]
    rollover_cost_dts = {_norm_dt(dt) for dt in raw_rollover_dts}

    route_change_dts = []
    prev_sym = None
    for bar in history_data_ref:
        dt = _norm_dt(bar.datetime)
        sym = bar_route_map.get(dt)
        if sym and prev_sym and sym != prev_sym:
            route_change_dts.append(dt)
        if sym:
            prev_sym = sym
    route_change_set = set(route_change_dts)

    audit_dts = rollover_cost_dts | route_change_set
    if not (bar_route_map and physical_bars_map and audit_dts and history_data_ref):
        return "<div class='empty-state'>未生成对账表：无换月事件，或映射/物理K线数据为空。</div>"

    try:
        is_daily = (engine.interval == VnpyInterval.DAILY)
    except Exception:
        is_daily = True
    offset_n = 2 if is_daily else 5

    dt_to_index = {_norm_dt(b.datetime): i for i, b in enumerate(history_data_ref)}
    target_indices = set()
    for rdt in audit_dts:
        idx = dt_to_index.get(rdt)
        if idx is not None:
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
            close_diff = f"<span class='diff-val'>{diff_val:+.2f}</span>"
        else:
            po = ph = pl = pc = "<span class='val-na'>N/A</span>"
            close_diff = "<span class='val-na'>N/A</span>"

        is_cost = dt in rollover_cost_dts
        is_route = dt in route_change_set
        if is_cost and is_route: flag, row_cls = "🔄 BOTH", "row-both"
        elif is_cost: flag, row_cls = "💰 ROLLOVER", "row-rollover"
        elif is_route: flag, row_cls = "🧭 ROUTE", "row-route"
        else: flag, row_cls = "", ""

        dt_str = dt.strftime("%Y-%m-%d %H:%M") if dt else "N/A"
        rows_html.append(f"""<tr class='{row_cls}'>
            <td class='mono'>{dt_str}</td>
            <td class='mono sym-cell'>{mapped_sym}</td>
            <td class='mono'>{bar.open_price:.2f}</td><td class='mono'>{bar.high_price:.2f}</td>
            <td class='mono'>{bar.low_price:.2f}</td><td class='mono'>{bar.close_price:.2f}</td>
            <td class='mono'>{po}</td><td class='mono'>{ph}</td>
            <td class='mono'>{pl}</td><td class='mono'>{pc}</td>
            <td class='mono'>{close_diff}</td>
            <td class='event-flag'>{flag}</td>
        </tr>""")

    return f"""
    <div class="section-card">
        <div class="section-header">
            <span class="section-icon">📐</span>
            <span>连续合约 K 线对账</span>
            <span class="section-sub">切换点 ±{offset_n} 根</span>
        </div>
        <p class="hint-line">close_diff = 连续复权收盘 − 物理原始收盘，复权后差值非零属正常。
        🔄 BOTH = 映射切换且有换月摩擦；💰 ROLLOVER = 有换月摩擦；🧭 ROUTE = 仅映射切换。</p>
        <div class="scroll-box">
            <table class="base-table mapping-table">
                <thead><tr>
                    <th>时间</th><th>物理合约</th>
                    <th>连续O</th><th>连续H</th><th>连续L</th><th>连续C</th>
                    <th>物理O</th><th>物理H</th><th>物理L</th><th>物理C</th>
                    <th>Close Diff</th><th>事件</th>
                </tr></thead>
                <tbody>{"".join(rows_html)}</tbody>
            </table>
        </div>
    </div>"""


def _build_rollover_audit(engine) -> str:
    rollover_logs = engine.get_rollover_logs()
    if not rollover_logs:
        return "<div class='empty-state'>本次回测未检测到换月摩擦。</div>"

    rows = []
    for log in rollover_logs:
        if log.get("status") == "FAILED":
            continue
        dt_str = log["datetime"].strftime("%Y-%m-%d")
        rows.append(f"""<tr>
            <td class='mono'>{dt_str}</td>
            <td class='mono sym-old'>{log['old_symbol']}</td>
            <td class='mono sym-new'>{log['new_symbol']}</td>
            <td>{log['direction']}</td>
            <td class='mono'>{log['volume']}</td>
            <td class='mono'>{log['ref_price']:.2f}</td>
            <td class='mono'>{log['commission']:.2f}</td>
            <td class='mono'>{log['slippage']:.2f}</td>
            <td class='mono pnl-neg'>{log['rollover_pnl']:.2f}</td>
        </tr>""")

    return f"""
    <table class="base-table">
        <thead><tr>
            <th>日期</th><th>旧合约</th><th>新合约</th><th>方向</th>
            <th>手数</th><th>基准价</th><th>手续费</th><th>滑点</th><th>摩擦损耗</th>
        </tr></thead>
        <tbody>{"".join(rows)}</tbody>
    </table>"""


def _build_orders_table(engine) -> str:
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
        rc = " class='row-cancelled'" if is_cancelled else ""
        badge = (f"<span class='reason-tag'>{o['reason']}</span>" if o["reason"] else "")
        dt_str = _normalize_dt(o["dt"]).strftime("%Y-%m-%d %H:%M")
        rows.append(f"<tr{rc}><td class='mono'>{dt_str}</td><td class='mono sym-cell'>{o['sym']}</td>"
                    f"<td>{o['type']}</td><td>{o['dir']}</td><td>{o['off']}</td>"
                    f"<td class='mono'>{o['price']:.2f}</td><td>{o['vol']}</td>"
                    f"<td>{o['status']}{badge}</td></tr>")

    return f"""<div class="scroll-box">
        <table class="base-table">
            <thead><tr><th>发单时间</th><th>合约</th><th>类型</th><th>方向</th>
            <th>动作</th><th>价格</th><th>数量</th><th>状态</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table></div>"""


def _build_trades_table(engine) -> str:
    trades = engine.get_all_trades()
    if not trades:
        return "<div class='empty-state'>无成交记录</div>"

    rows = []
    for t in trades:
        sym = getattr(t, "vt_symbol", f"{t.symbol}.{t.exchange.value}")
        rows.append(f"<tr><td class='mono'>{pd.to_datetime(t.datetime).strftime('%Y-%m-%d %H:%M')}</td>"
                    f"<td class='mono sym-cell'>{sym}</td><td>{t.direction.value}</td>"
                    f"<td>{t.offset.value}</td><td class='mono'>{t.price:.2f}</td>"
                    f"<td>{t.volume}</td></tr>")

    return f"""<div class="scroll-box">
        <table class="base-table">
            <thead><tr><th>成交时间</th><th>合约</th><th>方向</th>
            <th>动作</th><th>成交价</th><th>数量</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table></div>"""


def _build_daily_results_table(df) -> str:
    if df is None or df.empty:
        return "<div class='empty-state'>无日度结算数据</div>"

    rows = []
    for dt, row in df.sort_index(ascending=False).iterrows():
        pnl_cls = "pnl-pos" if row["net_pnl"] >= 0 else "pnl-neg"
        rows.append(f"<tr>"
                    f"<td class='mono'>{dt.strftime('%Y-%m-%d')}</td>"
                    f"<td class='mono'>{row['balance']:,.2f}</td>"
                    f"<td class='mono {pnl_cls}'>{row['net_pnl']:,.2f}</td>"
                    f"<td class='mono'>{row['commission']:,.2f}</td>"
                    f"<td class='mono'>{row['slippage']:,.2f}</td>"
                    f"<td class='mono'>{int(row['trade_count'])}</td>"
                    f"</tr>")

    return f"""
    <div class="scroll-box" style="max-height:420px">
        <table class="base-table">
            <thead><tr><th>日期</th><th>账户余额</th><th>当日净盈亏</th>
            <th>手续费</th><th>滑点</th><th>成交数</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>"""


# ──────────────────────────────────────────────────────────────────────────────
# CSS
# ──────────────────────────────────────────────────────────────────────────────

_STYLE = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
    --bg:       #0b0e18;
    --surface:  #111827;
    --surface2: #1a2235;
    --border:   #1e2d45;
    --border2:  #2a3f5c;
    --accent:   #3b82f6;
    --accent2:  #22d3ee;
    --gold:     #f59e0b;
    --red:      #f87171;
    --green:    #34d399;
    --text:     #e2e8f0;
    --muted:    #64748b;
    --muted2:   #475569;
    --font:     'IBM Plex Sans SC', 'PingFang SC', sans-serif;
    --mono:     'IBM Plex Mono', 'Fira Code', monospace;
}

html { scroll-behavior: smooth; }

body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    font-size: 13px;
    line-height: 1.6;
}

/* ── 侧边导航 ── */
.sidebar {
    position: fixed; left: 0; top: 0; bottom: 0;
    width: 200px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    padding: 24px 0;
    z-index: 100;
    display: flex; flex-direction: column;
}
.sidebar-logo {
    padding: 0 20px 20px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 12px;
}
.sidebar-logo .title { font-size: 0.8rem; font-weight: 700; color: var(--text); letter-spacing: 0.08em; }
.sidebar-logo .sub   { font-size: 0.65rem; color: var(--muted); margin-top: 2px; }
.nav-item {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 20px;
    font-size: 0.75rem; color: var(--muted);
    cursor: pointer; text-decoration: none;
    transition: color 0.15s, background 0.15s;
    border-left: 2px solid transparent;
}
.nav-item:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.nav-item.active { color: var(--accent); border-left-color: var(--accent); background: rgba(59,130,246,0.06); }
.nav-icon { width: 16px; text-align: center; opacity: 0.7; }

/* ── 主内容区 ── */
.main {
    margin-left: 200px;
    padding: 32px 32px 64px;
    max-width: 1280px;
}

/* ── 顶部标题栏 ── */
.page-header {
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 28px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
}
.page-header h1 { font-size: 1.1rem; font-weight: 600; color: var(--text); }
.header-badge {
    font-family: var(--mono); font-size: 0.65rem;
    padding: 3px 10px; border-radius: 20px;
    background: rgba(59,130,246,0.15); color: var(--accent);
    border: 1px solid rgba(59,130,246,0.3);
    letter-spacing: 0.05em;
}
.header-ts { margin-left: auto; font-size: 0.7rem; color: var(--muted); font-family: var(--mono); }

/* ── KPI 横条 ── */
.kpi-strip {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 12px;
    margin-bottom: 24px;
}
.kpi-cell {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    transition: border-color 0.15s;
}
.kpi-cell:hover { border-color: var(--border2); }
.kpi-label { font-size: 0.7rem; color: var(--muted); margin-bottom: 6px; }
.kpi-value { font-family: var(--mono); font-size: 1.1rem; font-weight: 600; color: var(--text); }
.kpi-pos   { color: var(--green) !important; }
.kpi-neg   { color: var(--red)   !important; }

/* ── 主布局 ── */
.layout-grid {
    display: grid;
    grid-template-columns: 1fr 280px;
    gap: 20px;
    align-items: start;
}
.layout-left  { min-width: 0; }
.layout-right { position: sticky; top: 32px; }

/* ── section 区块 ── */
.section-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 20px;
}
.section-header {
    display: flex; align-items: center; gap: 8px;
    padding: 11px 16px;
    border-bottom: 1px solid var(--border);
    background: rgba(255,255,255,0.015);
    font-size: 0.75rem; font-weight: 600;
    color: var(--text); letter-spacing: 0.04em;
}
.section-icon { font-size: 0.9rem; }
.section-sub  { margin-left: auto; font-size: 0.68rem; color: var(--muted); font-weight: 400; }

/* ── 绩效右侧面板 ── */
.stats-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    padding: 0 0 8px;
}
.stats-panel-header {
    padding: 11px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 0.75rem; font-weight: 600; color: var(--text);
    letter-spacing: 0.04em;
    background: rgba(255,255,255,0.015);
}
.stat-group-label {
    font-size: 0.65rem; color: var(--muted); letter-spacing: 0.08em;
    padding: 10px 16px 4px;
    text-transform: uppercase;
}
.stat-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 5px 16px;
    border-bottom: 1px solid rgba(255,255,255,0.03);
}
.stat-row:last-child { border-bottom: none; }
.stat-row.stat-highlight { background: rgba(245,158,11,0.04); }
.stat-key { font-size: 0.75rem; color: var(--muted); }
.stat-val { font-family: var(--mono); font-size: 0.78rem; color: var(--accent2); }
.stat-highlight .stat-key { color: var(--gold); }
.stat-highlight .stat-val { color: var(--gold); }

/* ── 基础表格 ── */
.base-table {
    width: 100%; border-collapse: collapse;
    font-size: 0.78rem;
}
.base-table th {
    position: sticky; top: 0; z-index: 2;
    background: #131c2e; color: var(--muted);
    font-weight: 600; font-size: 0.68rem;
    letter-spacing: 0.06em; text-transform: uppercase;
    padding: 8px 12px; white-space: nowrap; text-align: left;
    border-bottom: 1px solid var(--border);
}
.base-table td {
    padding: 6px 12px; white-space: nowrap;
    border-bottom: 1px solid rgba(255,255,255,0.03);
    color: var(--text);
}
.base-table tbody tr:hover td { background: rgba(59,130,246,0.06); }
.mono     { font-family: var(--mono); font-size: 0.76rem; }
.sym-cell { color: var(--accent2); }
.sym-old  { color: var(--red);     }
.sym-new  { color: var(--green);   }
.pnl-pos  { color: var(--green);   }
.pnl-neg  { color: var(--red);     }
.diff-val { color: var(--muted2);  }
.val-na   { color: var(--red); opacity: 0.7; }
.event-flag { font-weight: 600; }
.row-both     td { background: rgba(245,158,11,0.07)  !important; }
.row-rollover td { background: rgba(245,158,11,0.05)  !important; color: var(--gold) !important; }
.row-route    td { background: rgba(59,130,246,0.05)  !important; }
.row-cancelled td { color: var(--red) !important; opacity: 0.8; }

/* ── QA 表格 ── */
.qa-table   { width: 100%; }
.qa-label-zh { display: block; font-size: 0.78rem; color: var(--text); }
.qa-label-en { display: block; font-size: 0.68rem; color: var(--muted); margin-top: 1px; font-family: var(--mono); }
.qa-criterion { font-size: 0.7rem; color: var(--muted); }
.qa-badge {
    display: inline-block;
    font-family: var(--mono); font-size: 0.72rem; font-weight: 600;
    padding: 3px 10px; border-radius: 12px;
}
.qa-pass  { background: rgba(52,211,153,0.12); color: var(--green); border: 1px solid rgba(52,211,153,0.25); }
.qa-fail  { background: rgba(248,113,113,0.12); color: var(--red);   border: 1px solid rgba(248,113,113,0.25); }
.qa-warn  { background: rgba(245,158,11,0.12);  color: var(--gold);  border: 1px solid rgba(245,158,11,0.25); }
.qa-muted { background: rgba(100,116,139,0.12); color: var(--muted); border: 1px solid rgba(100,116,139,0.2); }

/* ── 杂项 ── */
.scroll-box     { max-height: 380px; overflow: auto; }
.hint-line      { font-size: 0.72rem; color: var(--muted); padding: 8px 14px; border-bottom: 1px solid var(--border); line-height: 1.5; }
.empty-state    { padding: 28px; text-align: center; color: var(--muted); font-size: 0.82rem; }
.section-pad    { padding: 16px; }
.reason-tag     { font-size: 0.65rem; background: rgba(245,158,11,0.15); color: var(--gold); padding: 1px 5px; border-radius: 3px; margin-left: 4px; }

/* 折叠面板 */
details.section-card > summary { list-style: none; cursor: pointer; outline: none; }
details.section-card > summary::-webkit-details-marker { display: none; }
"""

_SCRIPT = """
// 导航高亮
(function() {
    var sections = document.querySelectorAll('[data-section]');
    var navItems = document.querySelectorAll('.nav-item[href]');
    function onScroll() {
        var scrollY = window.scrollY + 80;
        var current = '';
        sections.forEach(function(s) {
            if (s.offsetTop <= scrollY) current = s.dataset.section;
        });
        navItems.forEach(function(n) {
            n.classList.toggle('active', n.getAttribute('href') === '#' + current);
        });
    }
    window.addEventListener('scroll', onScroll);
    onScroll();
})();
"""

# ──────────────────────────────────────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────────────────────────────────────


def generate_web_report(engine, df, stats, result_dir):
    # 构建各个 HTML 片段
    kpi_html = _build_kpi_strip(stats)
    qa_html = _build_qa_summary(engine)
    mapping_html = _build_mapping_table(engine)
    chart_html = _build_chart(engine, df)
    daily_html = _build_daily_results_table(df)
    stats_html = _build_stats_panel(stats)
    orders_html = _build_orders_table(engine)
    trades_html = _build_trades_table(engine)
    rollover_html = _build_rollover_audit(engine)
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CTA 回测报告</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans+SC:wght@400;600&display=swap" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>{_STYLE}</style>
</head>
<body>

<nav class="sidebar">
    <div class="sidebar-logo">
        <div class="title">CTA REPORT</div>
        <div class="sub">V1.2.1 · PHASE 1</div>
    </div>
    <a class="nav-item active" href="#qa"><span class="nav-icon">🛡</span>QA 审计</a>
    <a class="nav-item" href="#mapping"><span class="nav-icon">📐</span>K 线对账</a>
    <a class="nav-item" href="#chart"><span class="nav-icon">📈</span>收益图表</a>
    <a class="nav-item" href="#daily"><span class="nav-icon">📅</span>日度明细</a>
    <a class="nav-item" href="#rollover"><span class="nav-icon">🔄</span>换月审计</a>
    <a class="nav-item" href="#orders"><span class="nav-icon">📋</span>订单记录</a>
    <a class="nav-item" href="#trades"><span class="nav-icon">✅</span>成交记录</a>
</nav>

<div class="main">
    <div class="page-header">
        <h1>📊 CTA 回测验真报告</h1>
        <span class="header-badge">PHASE 1</span>
        <span class="header-ts">{ts_str}</span>
    </div>

    {kpi_html}

    <div class="layout-grid">
        <div class="layout-left">

            <div data-section="qa">{qa_html}</div>

            <div data-section="mapping">{mapping_html}</div>

            <div class="section-card" data-section="chart">
                <div class="section-header">
                    <span class="section-icon">📈</span>
                    <span>收益图表</span>
                    <span class="section-sub">净值 / 回撤 / 盈亏</span>
                </div>
                <div class="section-pad">{chart_html}</div>
            </div>

            <div class="section-card" data-section="daily">
                <div class="section-header">
                    <span class="section-icon">📅</span>
                    <span>日度结算明细</span>
                    <span class="section-sub">倒序排列</span>
                </div>
                <div>{daily_html}</div>
            </div>

            <div class="section-card" data-section="rollover">
                <div class="section-header">
                    <span class="section-icon">🔄</span>
                    <span>换月事件审计</span>
                    <span class="section-sub">摩擦成本账本</span>
                </div>
                <div class="section-pad">{rollover_html}</div>
            </div>

            <div class="section-card" data-section="orders">
                <div class="section-header">
                    <span class="section-icon">📋</span>
                    <span>订单生命周期</span>
                </div>
                <div>{orders_html}</div>
            </div>

            <div class="section-card" data-section="trades">
                <div class="section-header">
                    <span class="section-icon">✅</span>
                    <span>物理成交记录</span>
                </div>
                <div>{trades_html}</div>
            </div>

        </div>

        <div class="layout-right">
            <div class="stats-panel">
                <div class="stats-panel-header">📊 绩效指标</div>
                {stats_html}
            </div>
        </div>
    </div>
</div>

<script>{_SCRIPT}</script>
</body>
</html>"""

    report_file = os.path.join(result_dir, f"report_{datetime.now().strftime('%H%M%S')}.html")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html)
    webbrowser.open(f"file://{os.path.abspath(report_file)}")
    print(f"✅ 报告已生成: {report_file}")
