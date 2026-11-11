"""回测对话框：取数源隔离 + 图表窗口口径（离屏，不联网）。"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from pa_agent.backtest.models import BacktestConfig, BacktestResult  # noqa: E402
from pa_agent.data.base import KlineBar  # noqa: E402

_APP = None
STEP = 86_400_000
T0 = 1_760_000_000_000


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class _FakeSource:
    def __init__(self) -> None:
        self.connected = False
        self.subscribed: tuple[str, str] | None = None

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def subscribe(self, symbol: str, timeframe: str) -> None:
        self.subscribed = (symbol, timeframe)


def _ctx(kind: str = "ashare") -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(general=SimpleNamespace(last_data_source=kind)),
        data_source=object(),
    )


def _bars(n: int) -> list[KlineBar]:
    out = []
    for i in range(n):
        p = 10.0 + i * 0.1
        out.append(
            KlineBar(
                seq=n - i,
                ts_open=T0 + i * STEP,
                open=p,
                high=p + 0.2,
                low=p - 0.2,
                close=p + 0.05,
                volume=1000.0,
            )
        )
    return out


def test_make_run_source_subscribes_dialog_symbol_and_timeframe(monkeypatch: pytest.MonkeyPatch) -> None:
    from pa_agent.gui import backtest_dialog as mod

    fake = _FakeSource()
    monkeypatch.setattr(
        "pa_agent.data.factory.create_data_source", lambda kind: fake, raising=True
    )
    _app()
    dlg = mod.BacktestDialog(ctx=_ctx("ashare"), symbol="600000", timeframe="1m")
    source, err = dlg._make_run_source("600519", "1d")
    assert err == ""
    assert source is fake
    assert fake.connected is True
    assert fake.subscribed == ("600519", "1d")  # 不再沿用主界面周期
    dlg._run_source = fake
    dlg._release_run_source()
    assert fake.connected is False


def test_current_kind_falls_back_to_settings() -> None:
    from pa_agent.gui import backtest_dialog as mod

    _app()
    dlg = mod.BacktestDialog(ctx=_ctx("gold"), symbol="黄金9999", timeframe="1d")
    assert dlg._current_kind() == "gold"


def test_render_chart_uses_full_history_and_marks_window() -> None:
    from pa_agent.gui import backtest_dialog as mod

    _app()
    dlg = mod.BacktestDialog(ctx=_ctx(), symbol="601599", timeframe="1d")
    cfg = BacktestConfig(symbol="601599", timeframe="1d", bar_count=5)
    bars = _bars(60)
    result = BacktestResult(
        config=cfg,
        bars=tuple(reversed(bars)),
        window_bars=5,
    )
    dlg._render_chart(result)

    # 画的是整段历史，而不是只有回测的 5 根
    frame = dlg._chart.displayed_frame()
    assert frame is not None
    assert len(frame.bars) == 60
    overlay = dlg._chart._bt_overlay
    assert overlay is not None
    assert [s.kind for s in overlay.spans] == ["window"]
    assert overlay.spans[0].x1 == pytest.approx(59.5)

def test_steps_tab_lists_every_step_with_evidence() -> None:
    """决策步骤页签要把「不下单」的步也列出来，并带趋势/周期/评分。"""
    from pa_agent.backtest.engine import meta_from_payload
    from pa_agent.backtest.models import StepLog
    from pa_agent.gui import backtest_dialog as mod

    _app()
    dlg = mod.BacktestDialog(ctx=_ctx(), symbol="601599", timeframe="1d")
    payload = {
        "decision": {"order_type": "不下单", "reasoning": "区间中部", "diagnosis_confidence": 55, "trade_confidence": 40},
        "diagnosis": {
            "cycle_position": "trading_range",
            "trend_context": {"primary_direction": "neutral"},
            "market_phase": "transitioning",
        },
        "terminal": {"outcome": "wait", "label": "等待边界"},
    }
    meta = meta_from_payload(payload, payload["decision"])
    result = BacktestResult(
        config=BacktestConfig(symbol="601599", timeframe="1d", bar_count=1),
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
    dlg._fill_table(result)
    assert dlg._steps_table.rowCount() == 1
    texts = [
        dlg._steps_table.item(0, c).text() for c in range(dlg._steps_table.columnCount())
    ]
    assert texts[1] == "不下单"
    assert texts[8] == "交易区间"
    assert texts[7] == "中性"
    assert texts[9] == "转换中"
    assert texts[4] == "55"
    assert texts[5] == "40"