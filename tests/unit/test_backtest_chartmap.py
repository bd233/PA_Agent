"""回测成交 → 图表坐标映射 与 报告内嵌图的单测（不依赖 Qt）。"""
from __future__ import annotations

from types import SimpleNamespace

from pa_agent.backtest.chartmap import build_trade_overlay, x_for_ts
from pa_agent.backtest.models import BacktestConfig, BacktestResult
from pa_agent.backtest.report import render_html

T0 = 1_700_000_000_000
STEP = 86_400_000


def _bars(n: int = 6) -> list[dict]:
    """newest-first：最老在最后，最新在 [0]。"""
    bars = [
        {"ts_open": T0 + i * STEP, "open": 10.0 + i, "high": 11.0 + i, "low": 9.0 + i,
         "close": 10.5 + i, "volume": 1000.0}
        for i in range(n)
    ]
    return list(reversed(bars))


def _trade(**kw) -> SimpleNamespace:
    base = dict(
        plan_ts=T0 + STEP,
        entry_ts=T0 + 2 * STEP,
        exit_ts=T0 + 4 * STEP,
        direction="做多",
        order_type="限价单",
        entry=12.0,
        exit=14.0,
        stop=11.0,
        tp=14.5,
        tp2=None,
        qty=100.0,
        pnl=200.0,
        pnl_pct=0.02,
        exit_reason="止盈",
        equity_after=1_000_200.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_x_for_ts_maps_oldest_to_zero() -> None:
    ts_asc = [T0 + i * STEP for i in range(5)]
    assert x_for_ts(ts_asc[0], ts_asc) == 0.0
    assert x_for_ts(ts_asc[-1], ts_asc) == 4.0


def test_x_for_ts_clamps_and_rounds() -> None:
    ts_asc = [T0, T0 + STEP]
    assert x_for_ts(T0 - 10_000, ts_asc) == 0.0
    assert x_for_ts(T0 + STEP + 10_000, ts_asc) == 1.0
    # 更靠近左边 -> 取左
    assert x_for_ts(T0 + STEP // 3, ts_asc) == 0.0
    # 更靠近右边 -> 取右
    assert x_for_ts(T0 + STEP * 2 // 3, ts_asc) == 1.0


def test_empty_without_trades() -> None:
    assert build_trade_overlay(_bars(), []).is_empty


def test_long_trade_markers_and_lines() -> None:
    bars = _bars()
    overlay = build_trade_overlay(bars, [_trade()])

    assert len(overlay.markers) == 2
    entry, exit_ = overlay.markers
    # bars newest-first：T0+2 天是第 3 老的一根 → x=2；T0+4 天 → x=4
    assert (entry.x, entry.y, entry.kind, entry.label) == (2.0, 12.0, "entry", "买")
    assert (exit_.x, exit_.y, exit_.kind, exit_.label) == (4.0, 14.0, "exit", "平")

    kinds = [line.kind for line in overlay.lines]
    assert kinds == ["fill", "stop", "target"]
    stop = next(line for line in overlay.lines if line.kind == "stop")
    assert (stop.y0, stop.y1) == (11.0, 11.0)
    assert (stop.x0, stop.x1) == (2.0, 4.0)

    assert overlay.spans[0].x0 == 2.0 and overlay.spans[0].x1 == 4.0
    assert overlay.price_bounds == (11.0, 14.5)


def test_short_trade_label_and_color_hint() -> None:
    overlay = build_trade_overlay(_bars(), [_trade(direction="做空", exit=9.0)])
    entry = overlay.markers[0]
    assert entry.label == "卖"
    assert entry.direction == "做空"


def test_overlay_marks_backtest_window_even_without_trades() -> None:
    overlay = build_trade_overlay(_bars(), [], window_bars=3)
    assert not overlay.markers
    assert not overlay.is_empty
    window = overlay.spans[0]
    assert window.kind == "window"
    assert (window.x0, window.x1) == (2.5, 5.5)


def test_overlay_window_span_precedes_hold_span() -> None:
    overlay = build_trade_overlay(_bars(), [_trade()], window_bars=4)
    assert [span.kind for span in overlay.spans] == ["window", "hold"]
    assert overlay.spans[0].x0 == 1.5


def test_report_embeds_chart_when_png_given() -> None:
    cfg = BacktestConfig(symbol="600000", timeframe="1d", bar_count=3)
    result = BacktestResult(config=cfg)
    png = b"\x89PNG\r\n\x1a\nFAKE"
    page = render_html(result, chart_png=png)
    assert "data:image/png;base64," in page
    assert "K 线与成交点位" in page


def test_report_without_chart_has_no_image() -> None:
    cfg = BacktestConfig(symbol="600000", timeframe="1d", bar_count=3)
    result = BacktestResult(config=cfg)
    page = render_html(result)
    assert "data:image/png;base64," not in page
