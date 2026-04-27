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
import html

# ──────────────────────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────────────────────


def _normalize_dt(dt):
    if dt is None: return None
    ts = pd.Timestamp(dt)
    if ts.tzinfo is not None: ts = ts.tz_convert(None)
    return ts


def _norm_dt(dt):
    """剥离 tzinfo，兼容 pd.Timestamp。"""
    if dt is None:
        return None
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


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


def _build_order_datetime_map(engine) -> dict:
    """vt_orderid -> OrderData.datetime，用于让审计表的信号时间与回测订单时间对齐。"""
    mapping = {}
    try:
        for order in engine.get_all_orders():
            if getattr(order, "vt_orderid", None):
                mapping[order.vt_orderid] = order.datetime
    except Exception:
        pass
    return mapping


def _safe_dt_str(dt, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    ts = _normalize_dt(dt)
    return ts.strftime(fmt) if ts is not None else "N/A"


def _build_intent_audit_table(engine) -> str:
    """构建 V1.3 意图链路审计表。

    展示顺序调整为 Chain ID -> 信号时间；普通 Pipeline 记录优先使用真实订单时间，
    避免 SignalOrder.created_at 的机器时间与回测撮合时间不一致。
    """
    tracker = getattr(engine, "intent_tracker", None)
    if not tracker:
        return "<div class='empty-state'>暂无意图链路记录</div>"

    order_dt_map = _build_order_datetime_map(engine)
    all_records = list(tracker.chain_audit_map.values()) + tracker.chain_audit_archive
    rows = ""

    for r in all_records:
        sig, risk = r.get("signal"), r.get("risk")
        cid = sig.chain_id if sig else ""
        cid_display = html.escape(cid) if cid else "N/A"
        chain_id_attr = f'id="chain-{html.escape(cid)}"' if cid else ""

        if not r.get("orders"):
            # 风控拒单没有物理订单，使用 SignalOrder.created_at；回测里已由 send_order 注入 self.datetime。
            time_str = _safe_dt_str(getattr(sig, "created_at", None))
            reason = getattr(risk, "reject_reason", "RISK_REJECTED") if risk else "UNKNOWN"
            rows += f"""<tr class="audit-row-reject" {chain_id_attr}>
                <td class="mono chain-id-cell">{cid_display}</td>
                <td class="mono">{time_str}</td>
                <td>{html.escape(str(sig.direction.value if sig else 'N/A'))}/{html.escape(str(sig.offset.value if sig else 'N/A'))} {getattr(sig, 'volume', 'N/A')}@{getattr(sig, 'price', 'N/A')}</td>
                <td class="audit-risk-reject">{html.escape(str(risk.decision.value if risk else "N/A"))}</td>
                <td class="mono">[NO_ORDER]</td>
                <td class="audit-status-reject">Rejected</td>
                <td class="audit-reason">{html.escape(str(reason))}</td>
            </tr>"""
            continue

        first_row = True
        for ref in r.get("orders", []):
            # 普通链路的“信号时间”按真实订单时间展示，保证和订单生命周期表一致。
            time_str = _safe_dt_str(order_dt_map.get(ref.vt_orderid) or ref.updated_at or ref.created_at)
            status_str = ref.status.value if ref.status else "N/A"
            row_id_attr = f'id="chain-{html.escape(cid)}"' if first_row and cid else ""
            first_row = False
            order_id = html.escape(str(ref.vt_orderid)) if ref.vt_orderid else "N/A"
            order_link = f'<a class="chain-anchor" href="#order-{order_id}">{order_id}</a>' if ref.vt_orderid else "N/A"
            chain_html = f'<a class="chain-anchor" href="#order-{order_id}">{cid_display}</a>' if ref.vt_orderid and cid else cid_display

            rows += f"""<tr class="audit-row-pass" {row_id_attr}>
                <td class="mono chain-id-cell">{chain_html}</td>
                <td class="mono">{time_str}</td>
                <td>{html.escape(str(sig.direction.value if sig else 'N/A'))}/{html.escape(str(sig.offset.value if sig else 'N/A'))} {getattr(sig, 'volume', 'N/A')}@{getattr(sig, 'price', 'N/A')}</td>
                <td class="audit-risk-pass">{html.escape(str(risk.decision.value if risk else "N/A"))}</td>
                <td class="mono">{order_link}</td>
                <td>{html.escape(str(status_str))}</td>
                <td class="audit-reason">PIPELINE</td>
            </tr>"""

    for record in getattr(tracker, "exempt_trade_records", []):
        trade = record["trade"]
        reason = record.get("reason", "STOP_ORDER")
        time_str = _safe_dt_str(getattr(trade, "datetime", None))
        rows += f"""<tr class="audit-row-exempt">
            <td class="mono chain-id-cell" style="color: #f59e0b;">[EXEMPT]</td>
            <td class="mono">{time_str}</td>
            <td>{html.escape(str(trade.direction.value))}/{html.escape(str(trade.offset.value))} {trade.volume}@{trade.price}</td>
            <td class="audit-risk-exempt">N/A</td>
            <td class="mono">{html.escape(str(trade.vt_orderid))}</td>
            <td class="audit-status-exempt">All Traded</td>
            <td class="audit-reason">{html.escape(str(reason))}</td>
        </tr>"""

    if not rows:
        return "<div class='empty-state'>暂无意图链路记录</div>"

    return f"""<table class='base-table audit-table audit-table-wrap'>
        <thead><tr><th>Chain ID</th><th>信号时间</th><th>意图</th><th>风控</th><th>订单ID</th><th>状态</th><th>备注/来源</th></tr></thead>
        <tbody>{rows}</tbody></table>"""


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

    parts = []
    for group_name, keys in GROUPS:
        parts.append(f"<div class='stat-group-label'>{group_name}</div>")
        for label, k in keys:
            if k not in stats:
                continue
            val = _fmt_stat(k, stats[k])
            hl = " stat-highlight" if k in HIGHLIGHT else ""
            parts.append(f"""
            <div class="stat-row{hl}">
                <span class="stat-key">{label}</span>
                <span class="stat-val">{val}</span>
            </div>""")

    return "\n".join(parts)


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

    def _base_layout():
        return dict(
            height=320,
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            showlegend=False,
            margin=dict(l=55, r=20, t=16, b=50),
            font=dict(family="'IBM Plex Mono', monospace", size=11, color="#94a3b8"),
            xaxis=dict(type="category",
                       tickmode="array",
                       tickvals=sparse_x,
                       ticktext=sparse_x,
                       tickangle=-30,
                       showgrid=True,
                       gridcolor="rgba(255,255,255,0.05)"),
            yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.05)"),
        )

    fig1 = go.Figure(go.Scatter(x=x, y=net_value, mode="lines", name="净值", line=dict(color="#3b82f6", width=1.5)))
    fig1.update_layout(**_base_layout())

    fig2 = go.Figure(
        go.Scatter(x=x,
                   y=ddpercent,
                   fill="tozeroy",
                   name="回撤%",
                   line=dict(color="#f87171", width=1),
                   fillcolor="rgba(248,113,113,0.15)"))
    fig2.update_layout(**_base_layout())

    bar_colors = ["rgba(52,211,153,0.7)" if v >= 0 else "rgba(248,113,113,0.7)" for v in net_pnl]
    fig3 = go.Figure(go.Bar(x=x, y=net_pnl, name="日盈亏", marker_color=bar_colors))
    fig3.update_layout(**_base_layout())

    fig4 = go.Figure(
        go.Scatter(x=x,
                   y=cum_pnl,
                   fill="tozeroy",
                   name="累计盈亏",
                   line=dict(color="#22d3ee", width=1.5),
                   fillcolor="rgba(34,211,238,0.08)"))
    fig4.update_layout(**_base_layout())

    j1 = pio.to_json(fig1, engine="json")
    j2 = pio.to_json(fig2, engine="json")
    j3 = pio.to_json(fig3, engine="json")
    j4 = pio.to_json(fig4, engine="json")

    return f"""
    <div class="chart-tabs">
        <button class="chart-tab-btn active" data-tab="c_netval">📈 单位净值</button>
        <button class="chart-tab-btn" data-tab="c_dd">📉 回撤 %</button>
        <button class="chart-tab-btn" data-tab="c_daily">📊 每日盈亏</button>
        <button class="chart-tab-btn" data-tab="c_cum">💰 累计盈亏</button>
    </div>
    <div id="c_netval" class="chart-tab-pane active"><div id="chart_netval" style="width:100%;height:320px;"></div></div>
    <div id="c_dd"     class="chart-tab-pane"><div id="chart_dd" style="width:100%;height:320px;"></div></div>
    <div id="c_daily"  class="chart-tab-pane"><div id="chart_daily" style="width:100%;height:320px;"></div></div>
    <div id="c_cum"    class="chart-tab-pane"><div id="chart_cum" style="width:100%;height:320px;"></div></div>
    <script>
    (function(){{
        var charts = {{
            chart_netval: {j1},
            chart_dd:     {j2},
            chart_daily:  {j3},
            chart_cum:    {j4}
        }};
        Plotly.newPlot('chart_netval', charts.chart_netval.data, charts.chart_netval.layout, {{responsive:true, displayModeBar:false}});
        var rendered = {{chart_netval: true}};
        document.querySelectorAll('.chart-tab-btn').forEach(function(btn) {{
            btn.addEventListener('click', function() {{
                document.querySelectorAll('.chart-tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
                document.querySelectorAll('.chart-tab-pane').forEach(function(p) {{ p.classList.remove('active'); }});
                btn.classList.add('active');
                var tid = btn.dataset.tab;
                document.getElementById(tid).classList.add('active');
                var cid = 'chart_' + tid.replace('c_', '');
                if (!rendered[cid]) {{
                    Plotly.newPlot(cid, charts[cid].data, charts[cid].layout, {{responsive:true, displayModeBar:false}});
                    rendered[cid] = true;
                }}
            }});
        }});
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

    # ── 构建 Plotly 折线图替代表格 ──
    chart_x, chart_y, chart_text, marker_colors, marker_sizes, marker_symbols = [], [], [], [], [], []
    # 保留原始表格数据用于 hover
    table_rows_for_hover = []

    for idx in sorted(target_indices):
        bar = history_data_ref[idx]
        dt = _norm_dt(bar.datetime)
        mapped_sym = bar_route_map.get(dt, "MISSING")
        phys_bar = physical_bars_map.get((mapped_sym, dt))

        dt_str = dt.strftime("%Y-%m-%d") if dt else "N/A"
        if phys_bar:
            diff_val = bar.close_price - phys_bar.close_price
        else:
            diff_val = None

        is_cost = dt in rollover_cost_dts
        is_route = dt in route_change_set
        if is_cost and is_route:
            flag, color, size, sym_shape = "BOTH", "#22d3ee", 12, "diamond"
        elif is_cost:
            flag, color, size, sym_shape = "ROLLOVER", "#f59e0b", 10, "triangle-up"
        elif is_route:
            flag, color, size, sym_shape = "ROUTE", "#f87171", 10, "circle"
        else:
            flag, color, size, sym_shape = "", "#64748b", 6, "circle"

        if diff_val is not None:
            hover = f"{dt_str}<br>{mapped_sym}<br>Close Diff: {diff_val:+.2f}"
            if flag:
                hover += f"<br>事件: {flag}"
            chart_x.append(dt_str)
            chart_y.append(diff_val)
            chart_text.append(hover)
            marker_colors.append(color)
            marker_sizes.append(size)
            marker_symbols.append(sym_shape)

    if not chart_x:
        return "<div class='empty-state'>未生成对账图：无有效价差数据。</div>"

    line_trace = dict(type="scatter",
                      mode="lines+markers",
                      x=chart_x,
                      y=chart_y,
                      text=chart_text,
                      hoverinfo="text",
                      line=dict(color="rgba(59,130,246,0.5)", width=1),
                      marker=dict(color=marker_colors,
                                  size=marker_sizes,
                                  symbol=marker_symbols,
                                  line=dict(color="rgba(255,255,255,0.3)", width=1)),
                      name="Close Diff")

    zeroline = dict(type="scatter",
                    mode="lines",
                    x=[chart_x[0], chart_x[-1]],
                    y=[0, 0],
                    line=dict(color="rgba(255,255,255,0.15)", width=1, dash="dot"),
                    hoverinfo="skip",
                    showlegend=False)

    layout = dict(
        height=300,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        margin=dict(l=55, r=20, t=16, b=50),
        font=dict(family="'IBM Plex Mono', monospace", size=11, color="#94a3b8"),
        xaxis=dict(type="category", tickangle=-30, showgrid=True, gridcolor="rgba(255,255,255,0.05)"),
        yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.05)", title=dict(text="Close Diff", font=dict(size=10))),
        hovermode="closest",
    )

    import json as _json
    chart_json = _json.dumps({"data": [zeroline, line_trace], "layout": layout})

    legend_html = """<div class="mapping-legend">
        <span class="ml-item"><span class="ml-dot" style="background:#f87171;"></span>🧭 ROUTE</span>
        <span class="ml-item"><span class="ml-dot ml-tri" style="background:#f59e0b;"></span>💰 ROLLOVER</span>
        <span class="ml-item"><span class="ml-dot ml-dia" style="background:#22d3ee;"></span>🔄 BOTH</span>
        <span class="ml-item"><span class="ml-dot" style="background:#64748b;"></span>普通</span>
    </div>"""

    return f"""
    <div class="section-card">
        <div class="section-header">
            <span class="section-icon">📐</span>
            <span>连续合约 K 线对账</span>
            <span class="section-sub">切换点 ±{offset_n} 根 · Close Diff 走势</span>
        </div>
        <p class="hint-line">close_diff = 连续复权收盘 − 物理原始收盘，复权后差值非零属正常。悬停查看详情。</p>
        {legend_html}
        <div id="mapping_chart" style="width:100%;height:300px;"></div>
        <script>
        (function(){{
            var d = {chart_json};
            Plotly.newPlot('mapping_chart', d.data, d.layout, {{responsive:true, displayModeBar:false}});
        }})();
        </script>
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
    audit_logs = getattr(engine, "order_audit_logs", {})
    all_orders = []

    for o in engine.get_all_orders():
        audit = audit_logs.get(o.vt_orderid, {})
        all_orders.append({
            "dt": o.datetime,
            "sym": getattr(o, "vt_symbol", o.symbol),
            "type": "Limit",
            "dir": o.direction.value,
            "off": o.offset.value,
            "price": o.price,
            "vol": o.volume,
            "status": str(o.status.value),
            "source": audit.get("status_source", ""),
            "reason": audit.get("status_reason", ""),
            "vt_orderid": o.vt_orderid
        })

    for so in engine.get_all_stop_orders():
        reason = getattr(so, "cancel_reason", "")
        source = getattr(so, "cancel_source", "Strategy") if reason else ""
        all_orders.append({
            "dt": so.datetime,
            "sym": so.vt_symbol,
            "type": "Stop",
            "dir": so.direction.value,
            "off": so.offset.value,
            "price": so.price,
            "vol": so.volume,
            "status": str(so.status.value),
            "source": source,
            "reason": reason,
            "vt_orderid": getattr(so, "stop_orderid", "N/A")
        })

    for bo in getattr(engine, "warmup_blocked_orders", []):
        all_orders.append({
            "dt": bo["datetime"],
            "sym": "N/A",
            "type": bo["type"].capitalize(),
            "dir": "N/A",
            "off": "N/A",
            "price": 0.0,
            "vol": 0,
            "status": "BLOCKED",
            "source": "Engine_Interceptor",
            "reason": f"{bo['reason']} ({bo['phase']})",
            "vt_orderid": "N/A"
        })

    all_orders.sort(key=lambda x: _normalize_dt(x["dt"]) if x["dt"] else pd.Timestamp.min)
    if not all_orders: return "<div class='empty-state'>无委托记录</div>"

    tracker = getattr(engine, "intent_tracker", None)
    orderid_chain_map = getattr(tracker, "orderid_chain_map", {}) if tracker else {}
    limit_count = sum(1 for o in all_orders if o["type"] == "Limit")
    stop_count = sum(1 for o in all_orders if o["type"] == "Stop")
    abnormal_count = 0
    rows = []

    for o in all_orders:
        st_up = o["status"].upper()
        is_abnormal = ("撤销" in o["status"] or "拒单" in o["status"] or "CANCEL" in st_up or "REJECT" in st_up
                       or "BLOCKED" in st_up)
        if is_abnormal: abnormal_count += 1
        row_cls = "row-cancelled" if is_abnormal else "row-normal"

        badge = ""
        if o["reason"]: badge += f"<span class='reason-tag'>{html.escape(o['reason'])}</span>"
        if o["source"]: badge += f"<span class='source-tag'>{html.escape(o['source'])}</span>"

        dt_str = _normalize_dt(o["dt"]).strftime("%Y-%m-%d %H:%M") if o["dt"] is not None else "N/A"

        # 【反向锚点】点击跳往 Chain ID
        cid = orderid_chain_map.get(o["vt_orderid"], "") if o["vt_orderid"] != "N/A" else ""
        if o["type"] == "Stop":
            chain_cell = "<td class='mono'>[EXEMPT]</td>"
        elif cid:
            chain_cell = f'<td class="mono"><a class="chain-link" href="#chain-{cid}">{cid[:8]}...</a></td>'
        else:
            chain_cell = "<td class='mono muted'>—</td>"

        row_id_attr = f"id='order-{o['vt_orderid']}'" if o["vt_orderid"] != "N/A" else ""

        rows.append(
            f"<tr class='{row_cls}' data-type='{o['type'].lower()}' data-abnormal='{'true' if is_abnormal else 'false'}' {row_id_attr}>"
            f"{chain_cell}"
            f"<td class='mono'>{dt_str}</td><td class='mono sym-cell'>{html.escape(str(o['sym']))}</td>"
            f"<td>{o['type']}</td><td>{o['dir']}</td><td>{o['off']}</td>"
            f"<td class='mono'>{o['price']:.2f}</td><td>{o['vol']}</td>"
            f"<td>{html.escape(o['status'])}{badge}</td></tr>")

    return f"""
    <div class="table-filter-bar" data-filter-group="orders">
        <button class="filter-btn active" data-filter="all">全部 ({len(all_orders)})</button>
        <button class="filter-btn" data-filter="limit">限价单 ({limit_count})</button>
        <button class="filter-btn" data-filter="stop">止损单 ({stop_count})</button>
        <button class="filter-btn" data-filter="abnormal">异常/拦截 ({abnormal_count})</button>
    </div>
    <div class="scroll-box">
        <table class="base-table">
            <thead><tr><th>Chain ID</th><th>发单时间</th><th>合约</th><th>类型</th><th>方向</th>
            <th>动作</th><th>价格</th><th>数量</th><th>状态/审计归因</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>"""


def _build_trades_table(engine) -> str:
    trades = engine.get_all_trades()
    if not trades:
        return "<div class='empty-state'>无成交记录</div>"

    tracker = getattr(engine, "intent_tracker", None)
    orderid_chain_map = getattr(tracker, "orderid_chain_map", {}) if tracker else {}
    exempt_records = getattr(tracker, "exempt_trade_records", []) if tracker else []
    exempt_tradeids = {r["trade"].vt_tradeid for r in exempt_records}

    rows = []
    for t in trades:
        sym = getattr(t, "vt_symbol", f"{t.symbol}.{t.exchange.value}")

        if t.vt_tradeid in exempt_tradeids:
            chain_cell = "<td class='mono'>[EXEMPT]</td>"
        else:
            cid = orderid_chain_map.get(t.vt_orderid, "")
            # 【反向锚点】点击跳往 Chain ID
            chain_cell = f'<td class="mono"><a class="chain-link" href="#chain-{cid}">{cid[:8]}...</a></td>' if cid else "<td class='mono muted'>—</td>"

        rows.append(f"<tr>"
                    f"{chain_cell}"
                    f"<td class='mono'>{pd.to_datetime(t.datetime).strftime('%Y-%m-%d %H:%M')}</td>"
                    f"<td class='mono sym-cell'>{sym}</td><td>{t.direction.value}</td>"
                    f"<td>{t.offset.value}</td><td class='mono'>{t.price:.2f}</td>"
                    f"<td>{t.volume}</td></tr>")

    return f"""<div class="scroll-box">
        <table class="base-table">
            <thead><tr><th>Chain ID</th><th>成交时间</th><th>合约</th><th>方向</th>
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
# 公开接口
# ──────────────────────────────────────────────────────────────────────────────


def generate_web_report(engine, df, stats, result_dir):
    # 构建各个 HTML 碎片
    kpi_html = _build_kpi_strip(stats)
    qa_html = _build_qa_summary(engine)
    mapping_html = _build_mapping_table(engine)
    chart_html = _build_chart(engine, df)
    daily_html = _build_daily_results_table(df)
    stats_html = _build_stats_panel(stats)
    orders_html = _build_orders_table(engine)
    trades_html = _build_trades_table(engine)
    rollover_html = _build_rollover_audit(engine)
    intent_audit_html = _build_intent_audit_table(engine)
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 获取当前目录下的 templates 路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    tpl_dir = os.path.join(current_dir, "templates")

    # 读取前端资产文件
    with open(os.path.join(tpl_dir, "style.css"), "r", encoding="utf-8") as f:
        style_content = f.read()
    with open(os.path.join(tpl_dir, "script.js"), "r", encoding="utf-8") as f:
        script_content = f.read()
    with open(os.path.join(tpl_dir, "report_template.html"), "r", encoding="utf-8") as f:
        html_template = f.read()

    # 安全地进行占位符替换 (不使用 f-string 或 format 以防花括号冲突)
    html_output = html_template.replace("{{ STYLE_CONTENT }}", style_content) \
                               .replace("{{ SCRIPT_CONTENT }}", script_content) \
                               .replace("{{ TIMESTAMP }}", ts_str) \
                               .replace("{{ KPI_STRIP }}", kpi_html) \
                               .replace("{{ QA_HTML }}", qa_html) \
                               .replace("{{ MAPPING_HTML }}", mapping_html) \
                               .replace("{{ CHART_HTML }}", chart_html) \
                               .replace("{{ DAILY_HTML }}", daily_html) \
                               .replace("{{ ROLLOVER_HTML }}", rollover_html) \
                               .replace("{{ ORDERS_HTML }}", orders_html) \
                               .replace("{{ TRADES_HTML }}", trades_html) \
                               .replace("{{ STATS_HTML }}", stats_html)\
                               .replace("{{ INTENT_AUDIT_HTML }}", intent_audit_html)

    # 写入最终结果
    report_file = os.path.join(result_dir, f"report_{datetime.now().strftime('%H%M%S')}.html")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html_output)

    webbrowser.open(f"file://{os.path.abspath(report_file)}")
    print(f"✅ 报告已生成: {report_file}")
