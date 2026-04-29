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


def _short_id(x, n=8):
    """统一的 ID 截断函数，用于 UI 显示"""
    val = str(x or "")
    if len(val) > n:
        return html.escape(val[:n]) + "..."
    return html.escape(val or "N/A")


def _fmt_stat(key, val):
    """按指标类型格式化数值。"""
    PCT_KEYS = {"max_ddpercent", "total_return", "annual_return", "daily_return", "return_std"}
    RATIO_KEYS = {"sharpe_ratio", "ewm_sharpe", "return_drawdown_ratio", "rgr_ratio"}
    MONEY_KEYS = {
        "capital", "end_balance", "max_drawdown", "total_net_pnl", "daily_net_pnl", "total_commission", "daily_commission",
        "total_slippage", "daily_slippage", "total_turnover", "daily_turnover", "total_rollover_commission",
        "total_rollover_slippage", "all_in_commission", "all_in_slippage", "_gross_pnl"
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
    tracker = getattr(engine, "intent_tracker", None)
    if not tracker:
        return "<div class='empty-state'>暂无意图链路记录</div>"

    order_dt_map = _build_order_datetime_map(engine)
    all_records = list(getattr(tracker, "chain_audit_map", {}).values()) + getattr(tracker, "chain_audit_archive", [])
    rows = ""

    for r in all_records:
        sig, risk = r.get("signal"), r.get("risk")
        cid = sig.chain_id if sig else ""

        cid_display = _short_id(cid)

        decision = getattr(risk, "decision", None) if risk else None
        decision_name = getattr(decision, "name", "UNKNOWN").upper()

        if decision_name == "PASS":
            continue

        if decision_name == "REJECT":
            time_str = _safe_dt_str(getattr(sig, "created_at", None))
            reason = getattr(risk, "reject_reason", "RISK_REJECTED") if risk else "UNKNOWN"
            rows += f"""<tr class="audit-row-reject">
                <td class="mono chain-id-cell">{cid_display}</td>
                <td class="mono">{time_str}</td>
                <td>{html.escape(str(sig.direction.value if sig else 'N/A'))}/{html.escape(str(sig.offset.value if sig else 'N/A'))} {getattr(sig, 'volume', 'N/A')}@{getattr(sig, 'price', 'N/A')}</td>
                <td class="audit-risk-reject">REJECT</td>
                <td class="mono">[NO_ORDER]</td>
                <td class="audit-status-reject">Rejected</td>
                <td class="audit-reason">{html.escape(str(reason))}</td>
            </tr>"""
            continue

        if decision_name == "SHRINK":
            orig_vol = getattr(sig, 'volume', 'N/A')
            adj_vol = getattr(risk, 'adjusted_volume', 'N/A')
            # 渲染缩量摘要行（不 continue，继续渲染落地的子订单）
            rows += f"""<tr class="audit-row-shrink" style="border-bottom: none;">
                <td class="mono chain-id-cell">{cid_display}</td>
                <td class="mono">{_safe_dt_str(getattr(sig, "created_at", None))}</td>
                <td>{html.escape(str(sig.direction.value if sig else 'N/A'))}/{html.escape(str(sig.offset.value if sig else 'N/A'))} {orig_vol}@{getattr(sig, 'price', 'N/A')}</td>
                <td class="audit-risk-shrink">SHRINK</td>
                <td class="mono">[CAPACITY_ADJUST]</td>
                <td class="audit-status-shrink">Adjusting</td>
                <td class="audit-reason" style="color:#eab308; font-weight:bold;">
                    [SIZE_LIMIT] 意图:{orig_vol} ➔ 实际:{adj_vol}
                </td>
            </tr>"""

        # 渲染底层落地订单 / 未知状态 fallback
        first_row = True
        for ref in r.get("orders", []):
            time_str = _safe_dt_str(order_dt_map.get(ref.vt_orderid) or ref.updated_at or ref.created_at)
            status_str = html.escape(str(ref.status.value if ref.status else "N/A"))
            row_id_attr = f'id="chain-{html.escape(cid)}"' if first_row and cid else ""
            first_row = False
            order_id = html.escape(str(ref.vt_orderid)) if ref.vt_orderid else "N/A"
            order_link = f'<a class="chain-anchor" href="#order-{order_id}">{order_id}</a>' if ref.vt_orderid else "N/A"
            chain_html = f'<a class="chain-anchor" href="#order-{order_id}">↳ {cid_display}</a>' if ref.vt_orderid and cid else cid_display

            risk_css = "audit-risk-shrink" if decision_name == "SHRINK" else "audit-risk-pass"
            row_class = "audit-row-shrink-child" if decision_name == "SHRINK" else "audit-row-pass"

            rows += f"""<tr class="{row_class}" {row_id_attr}>
                <td class="mono chain-id-cell" style="padding-left: 20px; color:#94a3b8;">{chain_html}</td>
                <td class="mono">{time_str}</td>
                <td>{html.escape(str(sig.direction.value if sig else 'N/A'))}/{html.escape(str(sig.offset.value if sig else 'N/A'))} {getattr(sig, 'volume', 'N/A')}@{getattr(sig, 'price', 'N/A')}</td>
                <td class="{risk_css}">{decision_name}</td>
                <td class="mono">{order_link}</td>
                <td class="audit-status-pass">{status_str}</td>
                <td class="audit-reason">PIPELINE EXECUTED</td>
            </tr>"""

    # 豁免订单(止损单等)保留渲染
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
        return "<div class='empty-state'>暂无意图链路告警或豁免记录</div>"

    return f"""<table class='base-table audit-table audit-table-wrap'>
        <thead><tr><th>Chain ID</th><th>信号时间</th><th>意图</th><th>风控</th><th>订单ID</th><th>状态</th><th>备注/来源</th></tr></thead>
        <tbody>{rows}</tbody></table>"""


def _build_qa_summary(engine) -> str:
    from vnpy.trader.constant import Status as VnpyStatus

    get_orders_func = getattr(engine, "get_all_orders", None)
    all_orders = get_orders_func() if callable(get_orders_func) else []

    get_trades_func = getattr(engine, "get_all_trades", None)
    all_trades = get_trades_func() if callable(get_trades_func) else []

    get_logs_func = getattr(engine, "get_rollover_logs", None)
    rollover_logs = get_logs_func() if callable(get_logs_func) else getattr(engine, "rollover_logs", [])

    on_init_blocked = sum(1 for o in getattr(engine, "warmup_blocked_orders", []) if o.get("phase") == "on_init")
    rollover_failed = sum(1 for log in rollover_logs if log.get("status") == "FAILED")
    dl_failed = getattr(engine, "data_load_failed", False)

    norm_start = _norm_dt(getattr(engine, "start", None))
    trades_before_start = sum(1 for t in all_trades if t.datetime and norm_start and _norm_dt(t.datetime) < norm_start)

    audit_logs = getattr(engine, "order_audit_logs", {})
    rejected_missing = sum(1 for o in all_orders if getattr(o, "status", None) == VnpyStatus.REJECTED
                           and not audit_logs.get(getattr(o, "vt_orderid", ""), {}).get("status_reason"))
    cancelled_missing = sum(1 for o in all_orders if getattr(o, "status", None) == VnpyStatus.CANCELLED
                            and not audit_logs.get(getattr(o, "vt_orderid", ""), {}).get("status_reason"))
    audit_trail_missing = rejected_missing + cancelled_missing

    error_count = (1 if dl_failed else 0) + on_init_blocked + trades_before_start + rollover_failed + audit_trail_missing

    # 🟢 无论通过与否，统一生成明细结构
    rows_html = ""
    rows_html += f"<tr><td>Data Load 数据加载</td><td><span class='qa-badge {'qa-fail' if dl_failed else 'qa-pass'}'>{'FAIL' if dl_failed else 'PASS'}</span></td><td>底层数据链断裂</td></tr>"
    rows_html += f"<tr><td>on_init 非法发单</td><td><span class='qa-badge {'qa-fail' if on_init_blocked else 'qa-pass'}'>{on_init_blocked if on_init_blocked else '0'}</span></td><td>策略代码级 Bug</td></tr>"
    rows_html += f"<tr><td>回测前成交</td><td><span class='qa-badge {'qa-fail' if trades_before_start else 'qa-pass'}'>{trades_before_start if trades_before_start else '0'}</span></td><td>污染初始账本</td></tr>"
    rows_html += f"<tr><td>换月崩溃</td><td><span class='qa-badge {'qa-fail' if rollover_failed else 'qa-pass'}'>{rollover_failed if rollover_failed else '0'}</span></td><td>连续合约断链</td></tr>"
    rows_html += f"<tr><td>Audit Trail Missing</td><td><span class='qa-badge {'qa-fail' if audit_trail_missing else 'qa-pass'}'>{audit_trail_missing if audit_trail_missing else '0'}</span></td><td>无原因撤单/拒单</td></tr>"

    is_pass = (error_count == 0)
    detail_class = "qa-all-pass" if is_pass else "qa-fail-alert"
    icon = "✅" if is_pass else "🚨"
    title_text = "引擎核心 QA: 全部通过 (点击查看详情)" if is_pass else "引擎核心 QA: 异常告警 (点击展开详情)"
    badge_html = f"<span class='qa-badge qa-pass' style='margin-right: 12px;'>0 Exceptions</span>" if is_pass else f"<span class='qa-badge qa-fail' style='margin-right: 12px;'>{error_count} 项异常</span>"

    return f"""
    <div class="section-card" style="padding: 0; border: none; background: transparent;">
        <details class="qa-details {detail_class}">
            <summary>
                <div style="display:flex; align-items:center;">
                    <span class="section-icon" style="margin-right:8px;">{icon}</span>
                    <span>{title_text}</span>
                </div>
                {badge_html}
            </summary>
            <div class="qa-table-wrap">
                <table class="base-table qa-table" style="margin: 0; border-radius: 0; border: none;">
                    <thead><tr><th>检查项</th><th>异常计数</th><th>影响说明</th></tr></thead>
                    <tbody>{rows_html}</tbody>
                </table>
            </div>
        </details>
    </div>"""


def _aggregate_v15_metrics(engine, stats: dict) -> dict:
    """提取 V1.5 容量与约束指标 (三层架构标准数据字典)"""
    metrics = {
        "capital": {
            "reject_count": 0
        },
        "capacity": {
            "shrink_count": 0,
            "total_shrink_pct": 0.0
        },
        "execution": {
            "slippage_ratio": None,
            "gross_pnl": 0.0
        }
    }

    net_pnl = stats.get("total_net_pnl", 0)
    metrics["execution"]["gross_pnl"] = net_pnl + stats.get("all_in_commission", 0) + stats.get("all_in_slippage", 0)

    if metrics["execution"]["gross_pnl"] > 0:
        metrics["execution"]["slippage_ratio"] = stats.get("all_in_slippage", 0) / metrics["execution"]["gross_pnl"]

    tracker = getattr(engine, "intent_tracker", None)
    if tracker:
        raw_records = list(getattr(tracker, "chain_audit_map", {}).values()) + getattr(tracker, "chain_audit_archive", [])

        dedup_records = {}
        for i, r in enumerate(raw_records):
            sig = r.get("signal")
            key = sig.chain_id if (sig and getattr(sig, "chain_id", None)) else f"fallback_idx_{i}"
            dedup_records[key] = r

        shrink_sum = 0.0
        shrink_count = 0

        for r in dedup_records.values():
            risk = r.get("risk")
            if not risk: continue

            decision_name = getattr(risk.decision, "name", str(risk.decision)).upper()

            if decision_name == "REJECT":
                metrics["capital"]["reject_count"] += 1
            elif decision_name == "SHRINK":
                metrics["capacity"]["shrink_count"] += 1
                sig_vol = getattr(r.get("signal"), "volume", 0)
                adj_vol = getattr(risk, "adjusted_volume", sig_vol)

                if sig_vol > 0 and adj_vol < sig_vol:
                    shrink_sum += (1 - adj_vol / sig_vol)
                shrink_count += 1

        if shrink_count > 0:
            metrics["capacity"]["total_shrink_pct"] = (shrink_sum / shrink_count) * 100

    return metrics


def _build_config_strip(engine) -> str:
    """顶部展示 V1.5 物理回测假设"""
    slippage_model = None
    pipeline = getattr(engine, "order_pipeline", None)

    if pipeline:
        adapter = getattr(pipeline, "execution_adapter", None)
        if adapter:
            slippage_model = getattr(adapter, "slippage_model", None)

    if not slippage_model:
        slippage_model = getattr(engine, "slippage_model", None)

    slippage_cls = slippage_model.__class__.__name__ if slippage_model else "未知/默认模型"

    risk_mgr = getattr(pipeline, "risk_manager", None) if pipeline else None
    max_vol = getattr(risk_mgr, "max_order_size", "无限制")
    part_rate = getattr(risk_mgr, "max_participation_rate", "无限制")

    return f"""
    <div class="config-strip">
        <span class="config-item">滑点引擎: <span class="config-val">{slippage_cls}</span></span>
        <span class="config-item">最大单笔规模: <span class="config-val">{max_vol}</span></span>
        <span class="config-item">容量参与率上限: <span class="config-val">{part_rate}</span></span>
        <span class="config-item">回测阶段: <span class="config-val">V1.5 容量建模</span></span>
    </div>"""


def _build_health_panel(metrics: dict) -> str:
    """策略可执行性体检报告"""
    rej_count = metrics["capital"]["reject_count"]
    if rej_count > 0:
        cap_badge = f"<span class='health-badge health-red'>FAIL ({rej_count} 次)</span>"
        cap_desc = "策略触发资金拦截，资金管理逻辑失效或保证金不足。"
    else:
        cap_badge = "<span class='health-badge health-green'>PASS</span>"
        cap_desc = "未触发资金强制拒单。"

    shrink_count = metrics["capacity"]["shrink_count"]
    if shrink_count > 0:
        cap_badge_2 = f"<span class='health-badge health-yellow'>WARN ({shrink_count} 次)</span>"
        cap_desc_2 = f"触碰规模约束上限，发生订单被动裁剪。平均缩量比例: {metrics['capacity']['total_shrink_pct']:.1f}%"
    else:
        cap_badge_2 = "<span class='health-badge health-green'>PASS</span>"
        cap_desc_2 = "未触发风控规模约束与最大参与率上限。"

    slip_ratio = metrics["execution"]["slippage_ratio"]
    if slip_ratio is None:
        exe_badge = "<span class='health-badge health-yellow'>N/A</span>"
        exe_desc = "策略未产生正向毛收益，无法计算滑点侵蚀率。"
    else:
        slip_ratio_pct = slip_ratio * 100
        if slip_ratio_pct > 20:
            exe_badge = f"<span class='health-badge health-red'>{slip_ratio_pct:.1f}%</span>"
            exe_desc = "严重警告：滑点吃掉了超过20%的毛利润，动态冲击成本极高。"
        elif slip_ratio_pct > 5:
            exe_badge = f"<span class='health-badge health-yellow'>{slip_ratio_pct:.1f}%</span>"
            exe_desc = "滑点侵蚀处于正常冲击成本区间。"
        else:
            exe_badge = f"<span class='health-badge health-green'>{slip_ratio_pct:.1f}%</span>"
            exe_desc = "滑点侵蚀率极低，具备强执行可行性。"

    return f"""
    <div class="section-card">
        <div class="section-header">
            <span class="section-icon">🏥</span>
            <span>策略可执行性体检 (Capacity Health)</span>
            <span class="section-sub">Capital / Capacity / Execution</span>
        </div>
        <table class="base-table">
            <thead><tr><th>检查维度</th><th>体检结果</th><th>诊断说明</th></tr></thead>
            <tbody>
                <tr><td>Capital 资金健康</td><td>{cap_badge}</td><td>{cap_desc}</td></tr>
                <tr><td>Capacity 规模限制</td><td>{cap_badge_2}</td><td>{cap_desc_2}</td></tr>
                <tr><td>Execution 滑点侵蚀率</td><td>{exe_badge}</td><td>{exe_desc}</td></tr>
            </tbody>
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


def _build_stats_panel(stats: dict, metrics: dict) -> str:
    GROUPS = [
        ("盈亏表现", [
            ("总净盈亏", "total_net_pnl"),
            ("理论无摩擦盈亏", "_gross_pnl"),
            ("年化收益率", "annual_return"),
        ]),
        ("风险指标", [
            ("最大回撤", "max_drawdown"),
            ("最大回撤期(日)", "max_drawdown_duration"),
            ("夏普比率", "sharpe_ratio"),
            ("收益回撤比", "return_drawdown_ratio"),
            ("RGR Ratio", "rgr_ratio"),
        ]),
        ("成本摩擦 All-in", [
            ("策略成交笔数", "total_trade_count"),
            ("换月次数", "rollover_count"),
            ("综合手续费", "all_in_commission"),
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

    stats_copy = stats.copy()
    if metrics and "execution" in metrics:
        stats_copy["_gross_pnl"] = metrics["execution"]["gross_pnl"]

    parts = []
    for group_name, keys in GROUPS:
        parts.append(f"<div class='stat-group-label'>{group_name}</div>")
        for label, k in keys:
            if k not in stats_copy: continue

            # 使用 Flex column 拆解排版
            if k == "all_in_commission":
                val_main = _fmt_stat(k, stats_copy[k])
                val_s = _fmt_stat('total_commission', stats_copy.get('total_commission', 0))
                val_r = _fmt_stat('total_rollover_commission', stats_copy.get('total_rollover_commission', 0))
                val = f"<div class='stat-val-container'><div>{val_main}</div><div class='stat-breakdown'><span>策:{val_s}</span><span style='margin-left:8px'>换:{val_r}</span></div></div>"
            elif k == "all_in_slippage":
                val_main = _fmt_stat(k, stats_copy[k])
                val_s = _fmt_stat('total_slippage', stats_copy.get('total_slippage', 0))
                val_r = _fmt_stat('total_rollover_slippage', stats_copy.get('total_rollover_slippage', 0))
                val = f"<div class='stat-val-container'><div>{val_main}</div><div class='stat-breakdown'><span>策:{val_s}</span><span style='margin-left:8px'>换:{val_r}</span></div></div>"
            else:
                val = f"<div class='stat-val-container'><div>{_fmt_stat(k, stats_copy[k])}</div></div>"

            hl = " stat-highlight" if k in HIGHLIGHT else ""

            # 🟢 修复：将 span 升级为 div 容器，避免非法的 HTML 嵌套错位
            parts.append(f"""
            <div class="stat-row{hl}">
                <div class="stat-key">{label}</div>
                <div class="stat-val">{val}</div>
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

    x = [str(i) for i in plot_df.index]
    net_value = plot_df["net_value"].astype(float).tolist()
    ddpercent = plot_df["ddpercent"].astype(float).tolist()
    net_pnl = plot_df["net_pnl"].astype(float).tolist()

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

    j1 = pio.to_json(fig1, engine="json")
    j2 = pio.to_json(fig2, engine="json")
    j3 = pio.to_json(fig3, engine="json")

    return f"""
    <div class="chart-tabs">
        <button class="chart-tab-btn active" data-tab="c_netval">📈 单位净值</button>
        <button class="chart-tab-btn" data-tab="c_dd">📉 回撤 %</button>
        <button class="chart-tab-btn" data-tab="c_daily">📊 每日盈亏</button>
    </div>
    <div id="c_netval" class="chart-tab-pane active"><div id="chart_netval" style="width:100%;height:320px;"></div></div>
    <div id="c_dd"     class="chart-tab-pane"><div id="chart_dd" style="width:100%;height:320px;"></div></div>
    <div id="c_daily"  class="chart-tab-pane"><div id="chart_daily" style="width:100%;height:320px;"></div></div>
    <script>
    (function(){{
        var charts = {{ chart_netval: {j1}, chart_dd: {j2}, chart_daily: {j3} }};
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
    get_logs_func = getattr(engine, "get_rollover_logs", None)
    rollover_logs = get_logs_func() if callable(get_logs_func) else getattr(engine, "rollover_logs", [])

    # 计算静默预警指标
    mismatch_count = len(getattr(engine, "warmup_interval_mismatch_logs", []))
    missing_dts = []

    raw_rollover_dts = [log.get("datetime") for log in rollover_logs if log.get("status") not in ("FAILED", )]
    clean_rollover_dts = {_norm_dt(d) for d in raw_rollover_dts if d is not None}

    if clean_rollover_dts and hasattr(engine, "history_data"):
        dt_to_index = {
            _norm_dt(b.datetime): i
            for i, b in enumerate(getattr(engine, "history_data", [])) if hasattr(b, "datetime")
        }
        for rdt in clean_rollover_dts:
            if dt_to_index.get(rdt) is None:
                missing_dts.append(rdt)

    warnings = []
    if mismatch_count > 0: warnings.append(f"监测到 {mismatch_count} 次换月前后周期映射错配")
    if missing_dts: warnings.append(f"监测到 {len(missing_dts)} 处换月动作对应的物理 K 线缺失")

    warning_html = ""
    if warnings:
        warning_html = f"<div class='hint-line' style='color:#f59e0b; padding:8px; background:rgba(245,158,11,0.1); border-radius:4px; margin-bottom:8px;'>⚠️ 诊断发现: {' | '.join(warnings)}。</div>"

    valid_logs = [log for log in rollover_logs if log.get("status") != "FAILED"]
    if not valid_logs:
        return f"{warning_html}<div class='empty-state'>本次回测未检测到换月摩擦。</div>"

    rows = []
    for log in valid_logs:
        dt_raw = log.get("datetime")
        dt_str = dt_raw.strftime("%Y-%m-%d") if dt_raw else "N/A"

        rows.append(f"""<tr>
            <td class='mono'>{dt_str}</td>
            <td class='mono sym-old'>{log.get('old_symbol', 'N/A')}</td>
            <td class='mono sym-new'>{log.get('new_symbol', 'N/A')}</td>
            <td>{log.get('direction', 'N/A')}</td>
            <td class='mono'>{log.get('volume', 0)}</td>
            <td class='mono'>{log.get('ref_price', 0.0):.2f}</td>
            <td class='mono'>{log.get('commission', 0.0):.2f}</td>
            <td class='mono'>{log.get('slippage', 0.0):.2f}</td>
            <td class='mono pnl-neg'>{log.get('rollover_pnl', 0.0):.2f}</td>
        </tr>""")

    return f"""
    {warning_html}
    <div class="scroll-box" style="max-height: 400px;">
        <table class="base-table">
            <thead><tr>
                <th>日期</th><th>旧合约</th><th>新合约</th><th>方向</th>
                <th>手数</th><th>基准价</th><th>手续费</th><th>滑点</th><th>摩擦损耗</th>
            </tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>"""


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
    v15_metrics = _aggregate_v15_metrics(engine, stats)

    kpi_html = _build_kpi_strip(stats)
    config_html = _build_config_strip(engine)
    health_html = _build_health_panel(v15_metrics)
    qa_html = _build_qa_summary(engine)
    mapping_html = _build_mapping_table(engine)
    chart_html = _build_chart(engine, df)
    daily_html = _build_daily_results_table(df)
    stats_html = _build_stats_panel(stats, v15_metrics)
    orders_html = _build_orders_table(engine)
    trades_html = _build_trades_table(engine)
    rollover_html = _build_rollover_audit(engine)
    intent_audit_html = _build_intent_audit_table(engine)
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    tpl_dir = os.path.join(current_dir, "templates")

    with open(os.path.join(tpl_dir, "style.css"), "r", encoding="utf-8") as f:
        style_content = f.read()
    with open(os.path.join(tpl_dir, "script.js"), "r", encoding="utf-8") as f:
        script_content = f.read()
    with open(os.path.join(tpl_dir, "report_template.html"), "r", encoding="utf-8") as f:
        html_template = f.read()

    html_output = html_template.replace("{{ STYLE_CONTENT }}", style_content) \
                               .replace("{{ SCRIPT_CONTENT }}", script_content) \
                               .replace("{{ TIMESTAMP }}", ts_str) \
                               .replace("{{ KPI_STRIP }}", kpi_html) \
                               .replace("{{ CONFIG_STRIP }}", config_html) \
                               .replace("{{ HEALTH_PANEL_HTML }}", health_html) \
                               .replace("{{ QA_HTML }}", qa_html) \
                               .replace("{{ MAPPING_HTML }}", mapping_html) \
                               .replace("{{ CHART_HTML }}", chart_html) \
                               .replace("{{ DAILY_HTML }}", daily_html) \
                               .replace("{{ ROLLOVER_HTML }}", rollover_html) \
                               .replace("{{ ORDERS_HTML }}", orders_html) \
                               .replace("{{ TRADES_HTML }}", trades_html) \
                               .replace("{{ STATS_HTML }}", stats_html)\
                               .replace("{{ INTENT_AUDIT_HTML }}", intent_audit_html)

    report_file = os.path.join(result_dir, f"report_{datetime.now().strftime('%H%M%S')}.html")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(html_output)

    webbrowser.open(f"file://{os.path.abspath(report_file)}")
    print(f"✅ 报告已生成: {report_file}")
