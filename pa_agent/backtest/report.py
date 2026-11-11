"""HTML / CSV report rendering for a backtest run (no third-party deps)."""
from __future__ import annotations

import base64
import csv
import html
import io
from typing import Any

from pa_agent.backtest.models import BacktestResult, fmt_ts

_CSS = """
:root { color-scheme: dark; }
body { background:#0f1115; color:#d7dce5; font:13px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; margin:0; padding:24px 28px; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:15px; margin:26px 0 10px; padding-bottom:6px; border-bottom:1px solid #232a36; }
.sub { color:#7d8798; font-size:12px; margin-bottom:18px; }
.cards { display:flex; flex-wrap:wrap; gap:10px; }
.card { background:#161b23; border:1px solid #232a36; border-radius:8px; padding:10px 14px; min-width:132px; }
.card .k { color:#7d8798; font-size:11px; letter-spacing:.04em; }
.card .v { font-size:18px; font-weight:600; margin-top:2px; }
.pos { color:#ff5b6a; } .neg { color:#22c55e; }
img.chart { max-width:100%; border:1px solid #232a36; border-radius:6px; background:#0d1117; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { padding:6px 8px; border-bottom:1px solid #1e2531; text-align:right; white-space:nowrap; }
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2) { text-align:left; }
th { color:#8b95a7; font-weight:500; background:#141922; position:sticky; top:0; }
tr:hover td { background:#151b25; }
.note { color:#7d8798; font-size:12px; margin-top:8px; }
.kv { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:6px 18px; font-size:12px; }
.kv div { display:flex; justify-content:space-between; border-bottom:1px dashed #1e2531; padding:3px 0; }
.kv span:first-child { color:#8b95a7; }
code { color:#9fd1ff; }
"""


def _svg_curve(points: list[tuple[int, float]], width: int = 880, height: int = 220) -> str:
    """把权益曲线画成内联 SVG 折线（不引入 matplotlib）。"""
    if len(points) < 2:
        return '<p class="note">（没有成交，无资金曲线）</p>'
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    pad_y = max((y1 - y0) * 0.12, 1.0)
    lo, hi = y0 - pad_y, y1 + pad_y
    if hi == lo:
        hi = lo + 1

    def px(x: int) -> float:
        return 60 + (x - x0) / (x1 - x0) * (width - 90)

    def py(y: float) -> float:
        return height - 26 - (y - lo) / (hi - lo) * (height - 52)

    path = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in points)
    area = f"60,{height - 26} {path} {width - 30},{height - 26}"
    start = points[0][1]
    end = points[-1][1]
    color = "#ff5b6a" if end >= start else "#22c55e"
    grid = []
    for i in range(5):
        gy = 26 + i * (height - 52) / 4
        val = hi - (hi - lo) * i / 4
        grid.append(
            f'<line x1="60" y1="{gy:.1f}" x2="{width - 30}" y2="{gy:.1f}" stroke="#1e2531"/>'
            f'<text x="54" y="{gy + 3:.1f}" fill="#5c6678" font-size="10" text-anchor="end">'
            f"{val:,.0f}</text>"
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img">'
        f'<polygon points="{area}" fill="{color}" opacity="0.10"/>'
        + "".join(grid)
        + f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2"/>'
        + f'<circle cx="{px(xs[-1]):.1f}" cy="{py(ys[-1]):.1f}" r="3.5" fill="{color}"/>'
        + "</svg>"
    )


def _summary_cards(result: BacktestResult) -> str:
    s = result.summary()
    total = s["总盈亏"]
    cls = "pos" if total >= 0 else "neg"
    cards = [
        ("最终权益", f"{s['最终权益']:,.2f}", ""),
        ("总盈亏", f"{total:+,.2f}", cls),
        ("总收益率", f"{s['总收益率']:+.2%}", cls),
        ("胜率", f"{s['胜率']:.1%}", ""),
        ("下单次数", f"{s['下单次数']}", ""),
        ("盈亏比", "—" if s["盈亏比"] is None else f"{s['盈亏比']:.2f}", ""),
        ("最大回撤", f"{s['最大回撤']:.2%}", "neg" if s["最大回撤"] else ""),
        ("分析次数", f"{s['分析次数']}", ""),
    ]
    return "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v {c}">{v}</div></div>'
        for k, v, c in cards
    )


def _trade_table(result: BacktestResult) -> str:
    if not result.trades:
        return '<p class="note">本次回测没有产生任何成交（AI 全程未给可执行计划，或挂单未触发）。</p>'
    rows = []
    for t in result.trades:
        cls = "pos" if t.pnl >= 0 else "neg"
        rows.append(
            "<tr>"
            f"<td>{fmt_ts(t.plan_ts)}</td>"
            f"<td>{html.escape(t.direction)} / {html.escape(t.order_type)}</td>"
            f"<td>{t.entry:.3f}</td>"
            f"<td>{t.exit:.3f}</td>"
            f"<td>{t.stop:.3f}</td>"
            f"<td>{'-' if t.tp is None else f'{t.tp:.3f}'}</td>"
            f"<td>{t.qty:,.0f}</td>"
            f"<td>{t.bars_held}</td>"
            f"<td>{html.escape(t.exit_reason)}</td>"
            f'<td class="{cls}">{t.pnl:+,.2f}</td>'
            f'<td class="{cls}">{t.pnl_pct:+.2%}</td>'
            f"<td>{t.equity_after:,.2f}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>计划时间</th><th>方向/挂单</th><th>入场</th><th>出场</th><th>止损</th>"
        "<th>止盈</th><th>数量</th><th>持有K线</th><th>平仓原因</th><th>盈亏</th>"
        "<th>收益率</th><th>账户余额</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _step_table(result: BacktestResult, limit: int = 400) -> str:
    if not result.steps:
        return '<p class="note">（无步骤记录）</p>'
    rows = []
    for st in result.steps[:limit]:
        rows.append(
            "<tr>"
            f"<td>{fmt_ts(st.ts_open)}</td>"
            f"<td>{html.escape(st.order_type or '-')}</td>"
            f"<td>{html.escape(st.direction or '-')}</td>"
            f"<td>{'-' if st.entry is None else f'{st.entry:.3f}'}</td>"
            f"<td>{html.escape(st.note or '')}</td>"
            "</tr>"
        )
    more = ""
    if len(result.steps) > limit:
        more = f'<p class="note">仅显示前 {limit} 步，共 {len(result.steps)} 步。</p>'
    return (
        "<table><thead><tr><th>K线时间</th><th>挂单类型</th><th>方向</th>"
        "<th>计划入场</th><th>结果</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + more
    )


def _config_table(result: BacktestResult) -> str:
    items = result.config.to_dict()
    items["开始"] = result.started_iso or "-"
    items["结束"] = result.finished_iso or "-"
    items["标的"] = f"{result.config.symbol} · {result.config.timeframe}"
    if result.error:
        items["错误"] = result.error
    rows = "".join(
        f"<div><span>{html.escape(str(k))}</span><span>{html.escape(str(v))}</span></div>"
        for k, v in items.items()
    )
    return f'<div class="kv">{rows}</div>'


def _chart_section(chart_png: bytes | None, trade_count: int) -> str:
    """把图表截图内嵌进报告（没有截图时整节省略）。"""
    if not chart_png:
        return ""
    b64 = base64.b64encode(chart_png).decode("ascii")
    legend = (
        '<div class="note">红色 ▲ = 买入成交，绿色 ▼ = 做空成交，灰色 ■ = 平仓；'
        "浅蓝底 = 持仓区间，红点线 = 止损，绿点线 = 止盈，灰虚线 = 入场→出场。</div>"
    )
    return (
        f"<h2>K 线与成交点位（{trade_count} 笔）</h2>\n"
        f'<img class="chart" alt="K线与成交点位" src="data:image/png;base64,{b64}">\n'
        f"{legend}\n"
    )


def render_html(
    result: BacktestResult,
    *,
    title: str | None = None,
    chart_png: bytes | None = None,
) -> str:
    """生成自包含 HTML 报告（可直接双击打开、打印成 PDF）。

    *chart_png* 若给出，会以 data URI 内嵌「K 线 + 成交点位」截图。
    """
    cfg = result.config
    head = title or f"PA Agent 回测报告 · {cfg.symbol} {cfg.timeframe}"
    s = result.summary()
    cancelled = ' · <span class="neg">已取消（结果不完整）</span>' if result.cancelled else ""
    chart_block = _chart_section(chart_png, len(result.trades))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(head)}</title><style>{_CSS}</style></head>
<body>
<h1>{html.escape(head)}</h1>
<div class="sub">按 AI 建议逐根 K 线回放 · 生成于 {html.escape(result.finished_iso or '-')}{cancelled}</div>
<div class="cards">{_summary_cards(result)}</div>

{chart_block}
<h2>资金曲线</h2>
{_svg_curve(result.equity_curve)}

<h2>交易明细（{len(result.trades)} 笔）</h2>
{_trade_table(result)}

<h2>逐步记录（{len(result.steps)} 步）</h2>
{_step_table(result)}

<h2>回测参数</h2>
{_config_table(result)}

<h2>口径与限制</h2>
<div class="note">
① 每根 K 线收盘后调用一次两阶段 AI 分析，AI 只能看到该根及之前的数据（切片重建快照），不含未来信息。<br>
② 成交假设：限价单触及即成交（低开按开盘价）、突破单穿越即成交、市价单下一根开盘成交；挂单有效 {cfg.entry_valid_bars} 根。<br>
③ 出场：成交后从下一根开始逐根检查止损/止盈；<b>同一根内两者都命中按止损计</b>（保守）；区间结束未出场按最后收盘价平仓。<br>
④ 费用：佣金 {cfg.commission_rate:.4%}（双边）、印花税 {cfg.stamp_duty_rate:.4%}（卖出侧）、滑点 {cfg.slippage_rate:.4%}。<br>
⑤ 仓位：每次按当前权益的 {cfg.position_pct:.0%} 下单，按 {cfg.lot_size} 股/手向下取整；<b>未实现 T+1、涨跌停无法成交、停牌</b>——分钟级回测请自行打折看待。<br>
⑥ AI 判断存在随机性，同一段历史多次回测结果会有差异；样本量小（分析 {s['分析次数']} 次）时结论仅供方向参考。
</div>
</body></html>"""


def trades_to_csv(result: BacktestResult) -> str:
    """交易明细 CSV（UTF-8 BOM，Excel 直接可读）。"""
    buf = io.StringIO()
    fields: list[str] = []
    rows: list[dict[str, Any]] = [t.to_row() for t in result.trades]
    if rows:
        fields = list(rows[0].keys())
    else:
        fields = ["计划时间", "成交时间", "平仓时间", "方向", "挂单类型"]
    buf.write("\ufeff")
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()
