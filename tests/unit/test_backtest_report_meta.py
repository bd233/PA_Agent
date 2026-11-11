"""回测报告里的决策依据（趋势/周期/阶段/评分/理由）。"""
from __future__ import annotations

from pa_agent.backtest.engine import meta_from_payload
from pa_agent.backtest.models import BacktestConfig, BacktestResult, StepLog
from pa_agent.backtest.report import render_html, steps_to_csv
from pa_agent.backtest.runner import BacktestRunner
from pa_agent.data.base import KlineBar

STEP = 86_400_000
T0 = 1_760_000_000_000

PAYLOAD = {
    "decision": {
        "order_direction": None,
        "order_type": "不下单",
        "entry_price": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "reasoning": "价格在区间中部，等清晰边界。",
        "diagnosis_confidence": 55,
        "trade_confidence": 58,
        "estimated_win_rate": None,
        "key_factors": ["区间中部", "K1 缺确认"],
        "risk_assessment": "上下都可能被洗",
        "invalidation_condition": "收盘跌破 4.50",
    },
    "diagnosis": {
        "cycle_position": "trading_range",
        "alternative_cycle_position": "trending_tr",
        "direction": "neutral",
        "detected_patterns": ["middle_range", "overlap"],
        "trend_context": {"primary_direction": "neutral", "background_direction": "bullish"},
        "market_phase": "stable",
    },
    "terminal": {"node_id": "9.0P", "outcome": "wait", "label": "计划型限价不成立，等待清晰边界"},
}


def _bars(n: int = 20) -> list[KlineBar]:
    out = []
    price = 5.0
    for i in range(n):
        out.append(
            KlineBar(
                seq=n - i,
                ts_open=T0 + i * STEP,
                open=price,
                high=price + 0.1,
                low=price - 0.1,
                close=price + 0.02,
                volume=1000.0,
            )
        )
        price += 0.02
    return out


def test_meta_from_payload_extracts_zh_fields() -> None:
    meta = meta_from_payload(PAYLOAD, PAYLOAD["decision"])
    assert meta.trend == "中性"
    assert meta.trend_background == "偏多"
    assert meta.cycle == "交易区间"
    assert meta.alt_cycle == "趋势型交易区间"
    assert meta.phase == "稳定"
    assert meta.conf_diag == 55
    assert meta.conf_trade == 58
    assert meta.terminal_outcome == "wait"
    assert meta.terminal_label.startswith("计划型限价")
    assert meta.key_factors == ("区间中部", "K1 缺确认")
    assert not meta.is_empty


def test_runner_keeps_meta_for_every_step() -> None:
    cfg = BacktestConfig(symbol="002081", timeframe="1d", bar_count=2, analysis_bars=5)
    captured: dict[str, BacktestResult] = {}
    runner = BacktestRunner(config=cfg, bars=_bars(), analyze_fn=lambda _frame: PAYLOAD)
    runner.finished_ok.connect(lambda r: captured.setdefault("r", r))
    runner.run()

    result = captured["r"]
    assert len(result.steps) == 2
    for step in result.steps:
        assert step.meta is not None
        assert step.meta.cycle == "交易区间"
        assert step.meta.conf_diag == 55
        assert step.traded is False


def test_report_renders_decision_details() -> None:
    cfg = BacktestConfig(symbol="002081", timeframe="1d", bar_count=1)
    meta = meta_from_payload(PAYLOAD, PAYLOAD["decision"])
    result = BacktestResult(
        config=cfg,
        steps=[
            StepLog(
                ts_open=T0,
                order_type="不下单",
                direction="",
                entry=None,
                note="AI 未给单",
                traded=False,
                meta=meta,
            )
        ],
    )
    page = render_html(result)
    assert "判断依据（趋势 / 周期 / 阶段 / 理由）" in page
    assert "交易区间" in page
    assert "价格在区间中部" in page
    assert "区间中部" in page
    assert "平均诊断分" in page
    assert "55" in page


def test_steps_csv_has_decision_columns() -> None:
    cfg = BacktestConfig(symbol="002081", timeframe="1d", bar_count=1)
    meta = meta_from_payload(PAYLOAD, PAYLOAD["decision"])
    result = BacktestResult(
        config=cfg,
        steps=[
            StepLog(
                ts_open=T0,
                order_type="不下单",
                direction="",
                entry=None,
                note="AI 未给单",
                traded=False,
                meta=meta,
            )
        ],
    )
    text = steps_to_csv(result)
    assert text.startswith("\ufeff")
    assert "诊断分" in text and "周期" in text and "理由" in text
    assert "交易区间" in text
