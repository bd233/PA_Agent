"""回测引擎 / 报告 / 逐根回放的单元测试（不联网、不弹窗）。"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from pa_agent.backtest.engine import (
    equity_points,
    is_no_order,
    plan_from_decision,
    simulate_trade,
)
from pa_agent.backtest.models import (
    DIR_LONG,
    DIR_SHORT,
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    BacktestConfig,
    BacktestResult,
    SimTrade,
    StepLog,
)
from pa_agent.backtest.report import render_html, trades_to_csv

DAY = 86_400_000


@dataclass
class Bar:
    """引擎只需要 ts_open/open/high/low/close 属性。"""

    ts_open: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    closed: bool = True


def _cfg(**kw) -> BacktestConfig:
    base = dict(
        symbol="TEST",
        timeframe="1d",
        bar_count=5,
        initial_capital=100_000.0,
        lot_size=100,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        slippage_rate=0.0,
        entry_valid_bars=3,
    )
    base.update(kw)
    return BacktestConfig(**base)


def _plan(ts: int, **kw):
    dec = {
        "order_direction": DIR_LONG,
        "order_type": ORDER_LIMIT,
        "entry_price": 10.0,
        "stop_loss_price": 9.0,
        "take_profit_price": 12.0,
        "take_profit_price_2": 13.0,
        "estimated_win_rate": 55,
    }
    dec.update(kw)
    return plan_from_decision(ts, dec)


# ── plan 解析 ─────────────────────────────────────────────────────────────────

def test_plan_from_decision_reads_all_fields():
    plan = _plan(1_000)
    assert plan.entry == 10.0
    assert plan.stop == 9.0
    assert plan.tp == 12.0
    assert plan.tp2 == 13.0
    assert plan.win_rate == 55.0
    assert plan.is_actionable


@pytest.mark.parametrize(
    "dec",
    [
        {"order_direction": None, "order_type": "不下单"},
        {"order_direction": DIR_LONG, "order_type": "不下单", "entry_price": 10, "stop_loss_price": 9},
        {"order_direction": DIR_LONG, "order_type": ORDER_LIMIT, "entry_price": None, "stop_loss_price": 9},
        {"order_direction": DIR_LONG, "order_type": ORDER_LIMIT, "entry_price": 10, "stop_loss_price": 10.5},
        {"order_direction": DIR_SHORT, "order_type": ORDER_LIMIT, "entry_price": 10, "stop_loss_price": 9.5},
    ],
)
def test_plan_not_actionable(dec):
    assert not plan_from_decision(0, dec).is_actionable


def test_no_order_detection():
    assert is_no_order(plan_from_decision(0, {"order_type": "不下单"}))
    assert not is_no_order(_plan(0))


# ── 撮合与结算 ───────────────────────────────────────────────────────────────

def test_limit_long_fills_and_hits_take_profit():
    bars = [
        Bar(0, 10.2, 10.3, 9.8, 10.0),          # as-of
        Bar(DAY, 10.0, 10.1, 9.9, 10.0),        # 限价 10 触及
        Bar(2 * DAY, 10.2, 12.1, 10.1, 11.8),   # 触及 12 止盈
    ]
    trade = simulate_trade(_plan(0), bars, 0, _cfg(), capital=100_000)
    assert trade is not None
    assert trade.entry == pytest.approx(10.0)
    assert trade.exit == pytest.approx(12.0)
    assert trade.exit_reason == "止盈"
    assert trade.qty == 10_000  # 100000/10 取整到 100
    assert trade.pnl == pytest.approx(20_000, rel=1e-6)
    assert trade.hit_tp2 is False


def test_stop_loss_wins_when_both_hit_same_bar():
    bars = [
        Bar(0, 10.2, 10.3, 9.8, 10.0),
        Bar(DAY, 10.0, 10.05, 9.95, 10.0),      # 成交
        Bar(2 * DAY, 10.0, 12.5, 8.5, 9.2),     # 同根既触 12 又触 9
    ]
    trade = simulate_trade(_plan(0), bars, 0, _cfg(), capital=100_000)
    assert trade is not None
    assert trade.exit_reason == "止损"
    assert trade.exit == pytest.approx(9.0)
    assert trade.pnl == pytest.approx(-10_000, rel=1e-6)


def test_stop_gap_through_uses_open():
    bars = [
        Bar(0, 10.2, 10.3, 9.8, 10.0),
        Bar(DAY, 10.0, 10.05, 9.95, 10.0),
        Bar(2 * DAY, 8.0, 8.2, 7.8, 8.0),  # 跳空低开穿止损
    ]
    trade = simulate_trade(_plan(0), bars, 0, _cfg(), capital=100_000)
    assert trade is not None
    assert trade.exit == pytest.approx(8.0)
    assert trade.exit_reason == "止损"


def test_breakout_entry():
    bars = [
        Bar(0, 10.0, 10.2, 9.9, 10.0),
        Bar(DAY, 10.1, 10.6, 10.0, 10.5),
    ]
    plan = _plan(0, order_type=ORDER_STOP, entry_price=10.4, stop_loss_price=9.8, take_profit_price=12.0)
    trade = simulate_trade(plan, bars, 0, _cfg(), capital=100_000)
    assert trade is not None
    assert trade.entry == pytest.approx(10.4)
    assert trade.exit_reason == "到期平仓"
    assert trade.exit == pytest.approx(10.5)


def test_market_order_fills_next_open():
    bars = [
        Bar(0, 10, 10.1, 9.9, 10.0),
        Bar(DAY, 10.3, 10.4, 10.2, 10.35),
    ]
    plan = _plan(0, order_type=ORDER_MARKET, entry_price=10.0, stop_loss_price=9.0, take_profit_price=11.0)
    trade = simulate_trade(plan, bars, 0, _cfg(), capital=100_000)
    assert trade is not None
    assert trade.entry == pytest.approx(10.3)


def test_unfilled_order_expires():
    bars = [
        Bar(0, 10.0, 10.1, 9.9, 10.0),
        Bar(DAY, 10.5, 10.6, 10.4, 10.5),
        Bar(2 * DAY, 10.6, 10.7, 10.5, 10.6),
    ]
    plan = _plan(0, entry_price=10.0)  # 限价 10 一直没回踩
    assert simulate_trade(plan, bars, 0, _cfg(), capital=100_000) is None


def test_short_trade_pnl():
    bars = [
        Bar(0, 10.0, 10.1, 9.9, 10.0),
        Bar(DAY, 10.0, 10.05, 9.95, 10.0),
        Bar(2 * DAY, 9.9, 9.95, 8.9, 9.0),
    ]
    plan = _plan(
        0,
        order_direction=DIR_SHORT,
        entry_price=10.0,
        stop_loss_price=10.5,
        take_profit_price=9.0,
    )
    trade = simulate_trade(plan, bars, 0, _cfg(), capital=100_000)
    assert trade is not None
    assert trade.exit_reason == "止盈"
    assert trade.pnl == pytest.approx(10_000, rel=1e-6)


def test_fees_reduce_pnl():
    bars = [
        Bar(0, 10.2, 10.3, 9.8, 10.0),
        Bar(DAY, 10.0, 10.1, 9.9, 10.0),
        Bar(2 * DAY, 10.2, 12.1, 10.1, 11.8),
    ]
    cfg = _cfg(commission_rate=0.0003, stamp_duty_rate=0.001)
    trade = simulate_trade(_plan(0), bars, 0, cfg, capital=100_000)
    assert trade is not None
    assert trade.pnl < 20_000
    # 佣金 0.03% 双边 + 印花税 0.1% 卖出
    expected = 20_000 - 100_000 * 0.0003 - 120_000 * 0.0003 - 120_000 * 0.001
    assert trade.pnl == pytest.approx(expected, rel=1e-6)


def test_position_pct_limits_qty():
    bars = [
        Bar(0, 10.2, 10.3, 9.8, 10.0),
        Bar(DAY, 10.0, 10.1, 9.9, 10.0),
        Bar(2 * DAY, 10.2, 12.1, 10.1, 11.8),
    ]
    trade = simulate_trade(_plan(0), bars, 0, _cfg(position_pct=0.5), capital=100_000)
    assert trade is not None
    assert trade.qty == 5_000


def test_equity_points_start_and_order():
    trades = [
        SimTrade(0, DAY, 2 * DAY, DIR_LONG, ORDER_LIMIT, 10, 12, 9, 12, 13, 100,
                 200.0, 0.2, "止盈", 100_200.0),
        SimTrade(3 * DAY, 3 * DAY, 4 * DAY, DIR_LONG, ORDER_LIMIT, 10, 9, 9, 12, 13, 100,
                 -100.0, -0.1, "止损", 100_100.0),
    ]
    pts = equity_points(trades, 100_000.0)
    assert pts[0] == (0, 100_000.0)
    assert pts[-1] == (4 * DAY, 100_100.0)


# ── 报告 ─────────────────────────────────────────────────────────────────────

def _result() -> BacktestResult:
    cfg = _cfg()
    trade = SimTrade(
        0, DAY, 2 * DAY, DIR_LONG, ORDER_LIMIT, 10.0, 12.0, 9.0, 12.0, 13.0, 1000,
        2000.0, 0.2, "止盈", 102_000.0, hit_tp2=False, bars_held=1,
    )
    res = BacktestResult(config=cfg, trades=[trade])
    res.steps = [StepLog(0, ORDER_LIMIT, DIR_LONG, 10.0, "止盈 +2000.00", True)]
    res.equity_curve = [(0, 100_000.0), (2 * DAY, 102_000.0)]
    res.started_iso = res.finished_iso = "2026-11-11 18:00:00"
    return res


def test_render_html_contains_key_numbers():
    html = render_html(_result())
    assert "回测报告" in html
    assert "102,000.00" in html
    assert "止盈" in html
    assert "<svg" in html
    assert "T+1" in html  # 限制声明必须写明


def test_render_html_without_trades():
    res = BacktestResult(config=_cfg())
    res.equity_curve = [(0, 100_000.0)]
    html = render_html(res)
    assert "没有产生任何成交" in html


def test_trades_csv_has_bom_and_header():
    csv_text = trades_to_csv(_result())
    assert csv_text.startswith("\ufeff")
    assert "计划时间" in csv_text.splitlines()[0]
    assert "止盈" in csv_text


# ── 逐根回放 ─────────────────────────────────────────────────────────────────

def test_runner_walks_window_and_settles():
    from pa_agent.backtest.runner import BacktestRunner

    # 120 根渐升 K 线：保证每步都有足够历史构建 100 根快照
    bars = []
    for i in range(130):
        price = 10.0 + i * 0.02
        bars.append(
            Bar(
                ts_open=i * DAY,
                open=price,
                high=price + 0.05,
                low=price - 0.05,
                close=price,
            )
        )

    calls: list[int] = []

    def analyze(frame) -> dict:
        calls.append(int(frame.bars[0].ts_open))
        # 每隔一步给一次做多计划（入场在现价下方，保证下一根能成交）
        if len(calls) % 2 == 0:
            p = float(frame.bars[0].close)
            return {
                "order_direction": DIR_LONG,
                "order_type": ORDER_LIMIT,
                "entry_price": p,
                "stop_loss_price": p * 0.98,
                "take_profit_price": p * 1.02,
                "take_profit_price_2": p * 1.03,
                "estimated_win_rate": 50,
            }
        return {"order_direction": None, "order_type": "不下单"}

    cfg = _cfg(symbol="TEST", timeframe="1d", bar_count=10)
    runner = BacktestRunner(
        config=cfg,
        bars=bars,
        analyze_fn=analyze,
        cancel_token=None,
    )
    done: list[object] = []
    runner.finished_ok.connect(done.append)
    runner.run()  # 直接跑，避免依赖 Qt 事件循环

    assert done, "runner 应通过 finished_ok 返回结果"
    result = done[0]
    assert isinstance(result, BacktestResult)
    assert len(result.steps) == 10
    assert len(calls) == 10
    # 每步的快照都不含未来数据
    assert all(ts <= 129 * DAY for ts in calls)
    assert result.equity_curve
    assert result.summary()["分析次数"] == 10


def test_runner_reports_insufficient_history():
    from pa_agent.backtest.runner import BacktestRunner

    bars = [
        Bar(ts_open=i * DAY, open=10, high=10.1, low=9.9, close=10) for i in range(5)
    ]
    cfg = _cfg(bar_count=3, analysis_bars=100)
    runner = BacktestRunner(config=cfg, bars=bars, analyze_fn=lambda f: {}, cancel_token=None)
    done: list[object] = []
    runner.finished_ok.connect(done.append)
    runner.run()
    assert done
    assert done[0].skipped == 3
