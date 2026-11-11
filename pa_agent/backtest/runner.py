"""Walk-forward runner: for each historical bar, ask the AI, then settle the plan."""
from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from PyQt6.QtCore import QThread, pyqtSignal

from pa_agent.backtest.engine import (
    TERMINAL_OUTCOME_ZH,
    equity_points,
    is_no_order,
    meta_from_payload,
    plan_from_decision,
    simulate_trade,
    zh,
)
from pa_agent.backtest.models import (
    BacktestConfig,
    BacktestResult,
    StepLog,
    fmt_ts,
)

logger = logging.getLogger(__name__)

def _brief(meta: DecisionMeta | None) -> str:
    """进度日志里的简短判断摘要：周期 · 趋势 · 诊断分/交易分 · 阶段。"""
    if meta is None:
        return ""
    parts: list[str] = []
    if meta.cycle:
        parts.append(meta.cycle)
    if meta.trend:
        parts.append(meta.trend)
    if meta.conf_diag is not None or meta.conf_trade is not None:
        diag = "-" if meta.conf_diag is None else str(meta.conf_diag)
        trade = "-" if meta.conf_trade is None else str(meta.conf_trade)
        parts.append(f"诊断{diag}/交易{trade}")
    if meta.phase:
        parts.append(meta.phase)
    outcome = zh(meta.terminal_outcome, TERMINAL_OUTCOME_ZH)
    if outcome:
        parts.append(outcome)
    return f"（{' · '.join(parts)}）" if parts else ""

#: 连续多少次分析失败后提前终止（避免网络问题时空跑 N 次）
MAX_CONSECUTIVE_FAILURES = 3


def _closed_asc(bars: Sequence[Any]) -> list[Any]:
    """去掉未收盘的 K1，按时间升序返回（回测只在已收盘 K 线上推进）。"""
    out = [b for b in bars if bool(getattr(b, "closed", True))]
    out.sort(key=lambda b: int(getattr(b, "ts_open", 0)))
    return out


class BacktestRunner(QThread):
    """后台线程跑完整回测；结果通过信号回传，不阻塞界面。"""

    progress = pyqtSignal(int, int, str)  # (done, total, text)
    finished_ok = pyqtSignal(object)      # BacktestResult
    failed = pyqtSignal(str)

    def __init__(
        self,
        *,
        config: BacktestConfig,
        bars: Sequence[Any] | None = None,
        fetch_bars: Callable[[int], Sequence[Any]] | None = None,
        analyze_fn: Callable[[Any], dict[str, Any]],
        cancel_token: Any | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = config
        self._bars = list(bars or [])
        self._fetch_bars = fetch_bars
        self._analyze = analyze_fn
        self._cancel = cancel_token

    def _cancelled(self) -> bool:
        return bool(self._cancel is not None and self._cancel.is_set())

    def run(self) -> None:  # noqa: C901 - 主循环，分支已尽量扁平
        from datetime import datetime

        from pa_agent.data.snapshot import build_analysis_frame

        cfg = self._cfg
        result = BacktestResult(
            config=cfg, started_iso=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        try:
            if not self._bars and self._fetch_bars is not None:
                need = cfg.bar_count + cfg.analysis_bars + 60
                self.progress.emit(0, cfg.bar_count, f"拉取 K 线（需要 {need} 根）…")
                self._bars = list(self._fetch_bars(need))
            bars = _closed_asc(self._bars)
            if not bars:
                self.failed.emit("没有可用的已收盘 K 线")
                return

            window = bars[-cfg.bar_count :] if cfg.bar_count > 0 else bars
            total = len(window)
            if total <= 1:
                self.failed.emit("回测根数太少（至少 2 根）")
                return
            # 图表叠加用：把**拉取到的全部**已收盘 K 线转成 newest-first（图上能看到回测窗口以外的历史）
            result.bars = tuple(reversed(bars))
            result.window_bars = len(window)

            capital = float(cfg.initial_capital)
            consecutive_failures = 0

            for step, asof in enumerate(window):
                if self._cancelled():
                    result.cancelled = True
                    break
                asof_index = self._index_of(bars, asof)
                # as-of 切片：只给到该根为止（升序），转成 newest-first 交给快照构建
                slice_asc = bars[: asof_index + 1]
                if len(slice_asc) < cfg.analysis_bars:
                    result.skipped += 1
                    result.steps.append(
                        StepLog(
                            ts_open=int(asof.ts_open),
                            order_type="跳过",
                            direction="",
                            entry=None,
                            note=f"历史不足 {cfg.analysis_bars} 根",
                            traded=False,
                        )
                    )
                    self.progress.emit(step + 1, total, f"{fmt_ts(asof.ts_open)} 历史不足，跳过")
                    continue

                frame = build_analysis_frame(
                    list(reversed(slice_asc)),
                    cfg.analysis_bars,
                    cfg.symbol,
                    cfg.timeframe,
                )
                if frame is None:
                    result.skipped += 1
                    self.progress.emit(step + 1, total, f"{fmt_ts(asof.ts_open)} 快照构建失败，跳过")
                    continue

                try:
                    payload = self._analyze(frame) or {}
                    consecutive_failures = 0
                except Exception as exc:  # noqa: BLE001 - 单步失败不中断整轮回测
                    consecutive_failures += 1
                    result.failures += 1
                    logger.warning("Backtest step failed at %s: %s", fmt_ts(asof.ts_open), exc)
                    result.steps.append(
                        StepLog(
                            ts_open=int(asof.ts_open),
                            order_type="失败",
                            direction="",
                            entry=None,
                            note=str(exc)[:120],
                            traded=False,
                        )
                    )
                    self.progress.emit(
                        step + 1, total, f"{fmt_ts(asof.ts_open)} 分析失败：{str(exc)[:60]}"
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        result.error = f"连续 {consecutive_failures} 次分析失败，已提前终止"
                        break
                    continue

                # 兼容两种返回：完整分析包 {"decision","diagnosis","terminal"} 或单个 decision
                if isinstance(payload, dict) and "decision" in payload:
                    decision = payload.get("decision") or {}
                    meta = meta_from_payload(payload, decision)
                else:
                    decision = payload if isinstance(payload, dict) else {}
                    meta = meta_from_payload(None, decision)
                plan = plan_from_decision(int(asof.ts_open), decision)
                if is_no_order(plan):
                    result.steps.append(
                        StepLog(
                            ts_open=int(asof.ts_open),
                            order_type=plan.order_type or "不下单",
                            direction=plan.direction,
                            entry=plan.entry,
                            note="AI 未给单",
                            traded=False,
                            meta=meta,
                        )
                    )
                    self.progress.emit(
                        step + 1,
                        total,
                        f"{fmt_ts(asof.ts_open)} 不下单{_brief(meta)}",
                    )
                    continue

                trade = simulate_trade(plan, bars, asof_index, cfg, capital=capital)
                if trade is None:
                    result.steps.append(
                        StepLog(
                            ts_open=int(asof.ts_open),
                            order_type=plan.order_type,
                            direction=plan.direction,
                            entry=plan.entry,
                            note="挂单未成交/仓位不足",
                            traded=False,
                            meta=meta,
                        )
                    )
                    self.progress.emit(
                        step + 1,
                        total,
                        f"{fmt_ts(asof.ts_open)} {plan.direction}{plan.order_type} 未成交"
                        f"{_brief(meta)}",
                    )
                    continue

                capital = trade.equity_after
                result.trades.append(trade)
                result.steps.append(
                    StepLog(
                        ts_open=int(asof.ts_open),
                        order_type=plan.order_type,
                        direction=plan.direction,
                        entry=plan.entry,
                        note=f"{trade.exit_reason} {trade.pnl:+.2f}",
                        traded=True,
                        meta=meta,
                    )
                )
                self.progress.emit(
                    step + 1,
                    total,
                    f"{fmt_ts(asof.ts_open)} {plan.direction} 成交 {trade.entry:.2f} → "
                    f"{trade.exit:.2f}（{trade.exit_reason}）{trade.pnl:+.2f}"
                    f"{_brief(meta)}",
                )

            result.equity_curve = equity_points(result.trades, cfg.initial_capital)
            if not result.equity_curve:
                result.equity_curve = [(int(window[0].ts_open), cfg.initial_capital)]
            result.finished_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Backtest runner crashed")
            self.failed.emit(f"回测异常：{exc}")

    @staticmethod
    def _index_of(bars: Sequence[Any], bar: Any) -> int:
        """按对象身份定位下标（窗口来自 bars 切片，身份稳定）。"""
        for i, item in enumerate(bars):
            if item is bar:
                return i
        # 兜底：按 ts_open 查找
        ts = int(getattr(bar, "ts_open", 0))
        for i, item in enumerate(bars):
            if int(getattr(item, "ts_open", 0)) == ts:
                return i
        return len(bars) - 1


def make_analyze_fn(orchestrator: Any, cancel_token: Any) -> Callable[[Any], dict[str, Any]]:
    """把 TwoStageOrchestrator 包成 ``frame -> 分析结果包``。

    返回 ``{"decision": ..., "diagnosis": ..., "terminal": ...}``，
    这样回测报告除了「下不下单」还能看到趋势、周期、阶段、理由与评分。
    """

    def _analyze(frame: Any) -> dict[str, Any]:
        record = orchestrator.submit(frame, cancel_token, lambda _evt: None)
        if record is None:
            return {}
        s2 = getattr(record, "stage2_decision", None) or {}
        s1 = getattr(record, "stage1_diagnosis", None) or {}
        if not isinstance(s2, dict):
            return {}
        decision = s2.get("decision")
        return {
            "decision": decision if isinstance(decision, dict) else {},
            "diagnosis": s1 if isinstance(s1, dict) else {},
            "terminal": s2.get("terminal") if isinstance(s2.get("terminal"), dict) else {},
        }

    return _analyze


class NullPendingWriter:
    """回测专用：不落盘分析记录（否则会在 records/pending 堆 N 份 700KB JSON）。"""

    def save_full(self, record: Any) -> None:  # noqa: ARG002
        return None

    def save_partial(self, record: Any, reason: str) -> None:  # noqa: ARG002
        return None

    def append_followup(self, record_id: str, turn: Any) -> None:  # noqa: ARG002
        return None
