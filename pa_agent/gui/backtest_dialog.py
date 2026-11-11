"""回测对话框：配置参数 → 逐根回放 AI 决策 → 展示结果 → 保存报告。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from pa_agent.backtest.models import BacktestConfig, BacktestResult
from pa_agent.backtest.report import render_html, trades_to_csv
from pa_agent.backtest.runner import BacktestRunner, NullPendingWriter, make_analyze_fn

logger = logging.getLogger(__name__)

#: 每根 K 线一次两阶段调用，给出粗略耗时提示（秒/次）
_SECONDS_PER_STEP = 40


class BacktestDialog(QDialog):
    """模态回测窗口；关闭时若仍在跑会先取消。"""

    def __init__(
        self,
        *,
        ctx: Any,
        symbol: str,
        timeframe: str,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._runner: BacktestRunner | None = None
        self._result: BacktestResult | None = None

        self.setWindowTitle("回测（按 AI 建议逐根回放）")
        self.resize(960, 720)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # ── 参数区 ────────────────────────────────────────────────────────────
        form_box = QGroupBox("回测参数")
        form = QFormLayout(form_box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._symbol_edit = QLineEdit(symbol)
        self._symbol_edit.setPlaceholderText("留空则用当前标的")
        form.addRow("标的", self._symbol_edit)

        self._tf_combo = QComboBox()
        for tf in self._timeframe_choices(timeframe):
            self._tf_combo.addItem(tf)
        if timeframe and self._tf_combo.findText(timeframe) < 0:
            self._tf_combo.addItem(timeframe)
            self._tf_combo.setCurrentText(timeframe)
        form.addRow("周期", self._tf_combo)

        self._bars_spin = QSpinBox()
        self._bars_spin.setRange(2, 2000)
        self._bars_spin.setValue(30)
        self._bars_spin.setSingleStep(5)
        self._bars_spin.valueChanged.connect(self._update_estimate)
        form.addRow("回测K线数 N", self._bars_spin)

        self._capital_spin = QDoubleSpinBox()
        self._capital_spin.setRange(1000.0, 1e12)
        self._capital_spin.setDecimals(2)
        self._capital_spin.setSingleStep(100_000.0)
        self._capital_spin.setValue(1_000_000.0)
        self._capital_spin.setGroupSeparatorShown(True)
        form.addRow("初始资金", self._capital_spin)

        self._position_spin = QSpinBox()
        self._position_spin.setRange(1, 100)
        self._position_spin.setValue(100)
        self._position_spin.setSuffix(" %")
        self._position_spin.setToolTip("每次下单使用当前权益的比例")
        form.addRow("单笔仓位", self._position_spin)

        adv = QGroupBox("费用与撮合（默认 A 股口径，黄金可把印花税设为 0）")
        adv.setCheckable(True)
        adv.setChecked(False)
        adv_form = QFormLayout(adv)
        self._commission_spin = QDoubleSpinBox()
        self._commission_spin.setRange(0.0, 1.0)
        self._commission_spin.setDecimals(5)
        self._commission_spin.setSingleStep(0.0001)
        self._commission_spin.setValue(0.0003)
        adv_form.addRow("佣金率", self._commission_spin)
        self._stamp_spin = QDoubleSpinBox()
        self._stamp_spin.setRange(0.0, 1.0)
        self._stamp_spin.setDecimals(5)
        self._stamp_spin.setSingleStep(0.0001)
        self._stamp_spin.setValue(0.001)
        adv_form.addRow("印花税率（卖出）", self._stamp_spin)
        self._slip_spin = QDoubleSpinBox()
        self._slip_spin.setRange(0.0, 0.05)
        self._slip_spin.setDecimals(5)
        self._slip_spin.setValue(0.0)
        adv_form.addRow("滑点", self._slip_spin)
        self._valid_spin = QSpinBox()
        self._valid_spin.setRange(1, 50)
        self._valid_spin.setValue(5)
        adv_form.addRow("挂单有效K线数", self._valid_spin)
        self._lot_spin = QSpinBox()
        self._lot_spin.setRange(1, 1000)
        self._lot_spin.setValue(100)
        adv_form.addRow("每手股数", self._lot_spin)
        form.addRow(adv)

        layout.addWidget(form_box)

        # ── 提示与进度 ────────────────────────────────────────────────────────
        self._hint = QLabel()
        self._hint.setStyleSheet("color:#8b95a7;")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        # ── 按钮 ──────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("开始回测")
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn)
        self._stop_btn = QPushButton("停止")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self._stop_btn)
        btn_row.addStretch(1)
        self._save_btn = QPushButton("保存报告")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        # ── 结果 ──────────────────────────────────────────────────────────────
        self._summary = QLabel("尚未运行。")
        self._summary.setWordWrap(True)
        f = QFont()
        f.setPointSize(10)
        self._summary.setFont(f)
        layout.addWidget(self._summary)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["计划时间", "方向/挂单", "入场", "出场", "持有", "原因", "盈亏", "余额"]
        )
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, 1)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setPlaceholderText("进度日志…")
        layout.addWidget(self._log)

        self._update_estimate()

    # ── 小工具 ────────────────────────────────────────────────────────────────
    def _timeframe_choices(self, current: str) -> list[str]:
        source = getattr(self._ctx, "data_source", None)
        try:
            choices = list(source.supported_timeframes()) if source is not None else []
        except Exception:  # noqa: BLE001
            choices = []
        if not choices:
            choices = [current or "1d"]
        return choices

    def _update_estimate(self) -> None:
        n = self._bars_spin.value()
        minutes = n * _SECONDS_PER_STEP / 60.0
        self._hint.setText(
            f"将逐根回放最近 {n} 根 K 线：每根调用一次两阶段 AI 分析（约 "
            f"{_SECONDS_PER_STEP} 秒/次），预计 {minutes:.0f} 分钟；"
            f"中途可停止，已完成部分仍可保存报告。"
        )

    def _append_log(self, text: str) -> None:
        self._log.append(text)
        self._log.verticalScrollBar().setValue(self._log.verticalScrollBar().maximum())

    # ── 运行 ──────────────────────────────────────────────────────────────────
    def _on_start(self) -> None:
        if self._runner is not None and self._runner.isRunning():
            return
        symbol = self._symbol_edit.text().strip()
        if not symbol:
            QMessageBox.warning(self, "缺少标的", "请填写要回测的标的代码/名称。")
            return
        source = getattr(self._ctx, "data_source", None)
        if source is None:
            QMessageBox.warning(self, "数据源未就绪", "请先在主界面选择数据来源并加载数据。")
            return

        cfg = BacktestConfig(
            symbol=symbol,
            timeframe=self._tf_combo.currentText().strip() or "1d",
            bar_count=self._bars_spin.value(),
            initial_capital=float(self._capital_spin.value()),
            position_pct=self._position_spin.value() / 100.0,
            commission_rate=float(self._commission_spin.value()),
            stamp_duty_rate=float(self._stamp_spin.value()),
            slippage_rate=float(self._slip_spin.value()),
            entry_valid_bars=self._valid_spin.value(),
            lot_size=self._lot_spin.value(),
        )

        orchestrator = self._build_orchestrator()
        if orchestrator is None:
            QMessageBox.warning(
                self, "AI 未就绪", "编排器构建失败，请检查 API Key 与模型设置。"
            )
            return

        from pa_agent.util.threading import CancelToken

        token = CancelToken()
        analyze = make_analyze_fn(orchestrator, token)

        self._result = None
        self._table.setRowCount(0)
        self._summary.setText("运行中…")
        self._log.clear()
        self._progress.setRange(0, cfg.bar_count)
        self._progress.setValue(0)
        self._save_btn.setEnabled(False)
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)

        runner = BacktestRunner(
            config=cfg,
            fetch_bars=lambda need: source.latest_snapshot(need),
            analyze_fn=analyze,
            cancel_token=token,
            parent=self,
        )
        self._runner = runner
        self._cancel_token = token
        runner.progress.connect(self._on_progress)
        runner.finished_ok.connect(self._on_finished)
        runner.failed.connect(self._on_failed)
        runner.start()

    def _build_orchestrator(self) -> Any:
        """复用主窗口组件，但换成不落盘 Writer（回测记录不污染 pending）。"""
        try:
            from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

            client = getattr(self._ctx, "client", None)
            assembler = getattr(self._ctx, "assembler", None)
            router = getattr(self._ctx, "router", None)
            validator = getattr(self._ctx, "validator", None)
            exp_reader = getattr(self._ctx, "exp_reader", None)
            settings = getattr(self._ctx, "settings", None)
            if any(x is None for x in (client, assembler, router, validator, exp_reader)):
                return None
            return TwoStageOrchestrator(
                client=client,
                assembler=assembler,
                router=router,
                validator=validator,
                pending_writer=NullPendingWriter(),
                exp_reader=exp_reader,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Backtest orchestrator build failed: %s", exc)
            return None

    def _on_progress(self, done: int, total: int, text: str) -> None:
        if total > 0:
            self._progress.setRange(0, total)
            self._progress.setValue(done)
            self._progress.setFormat(f"%v / %m（{done * 100 // max(total, 1)}%）")
        self._append_log(text)

    def _on_finished(self, result: BacktestResult) -> None:
        self._result = result
        self._finish_ui()
        s = result.summary()
        tail = "（已取消，结果不完整）" if result.cancelled else ""
        self._summary.setText(
            f"完成{tail}：分析 {s['分析次数']} 次 · 下单 {s['下单次数']} 次 · 胜率 "
            f"{s['胜率']:.1%} · 盈亏 {s['总盈亏']:+,.2f} · 收益率 {s['总收益率']:+.2%} · "
            f"最大回撤 {s['最大回撤']:.2%}"
            + (f" · {result.error}" if result.error else "")
        )
        self._fill_table(result)

    def _on_failed(self, message: str) -> None:
        self._finish_ui()
        self._summary.setText(message)
        self._append_log(f"✗ {message}")

    def _on_stop(self) -> None:
        token = getattr(self, "_cancel_token", None)
        if token is not None:
            token.set()
        self._append_log("已请求停止，等当前这一步分析返回…")
        self._stop_btn.setEnabled(False)

    def _finish_ui(self) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._save_btn.setEnabled(self._result is not None)

    def _fill_table(self, result: BacktestResult) -> None:
        self._table.setRowCount(0)
        for t in result.trades:
            row = self._table.rowCount()
            self._table.insertRow(row)
            cells = [
                datetime.fromtimestamp(t.plan_ts / 1000).strftime("%m-%d %H:%M"),
                f"{t.direction}/{t.order_type}",
                f"{t.entry:.3f}",
                f"{t.exit:.3f}",
                f"{t.bars_held}",
                t.exit_reason,
                f"{t.pnl:+,.2f}",
                f"{t.equity_after:,.2f}",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col >= 6:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if col == 6:
                    item.setForeground(
                        Qt.GlobalColor.red if t.pnl >= 0 else Qt.GlobalColor.green
                    )
                self._table.setItem(row, col, item)

    # ── 保存 ──────────────────────────────────────────────────────────────────
    def _on_save(self) -> None:
        if self._result is None:
            return
        cfg = self._result.config
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        default = str(Path.home() / f"backtest_{cfg.symbol}_{cfg.timeframe}_{stamp}.html")
        path, _ = QFileDialog.getSaveFileName(
            self, "保存回测报告", default, "HTML 报告 (*.html);;所有文件 (*)"
        )
        if not path:
            return
        html_path = Path(path)
        if html_path.suffix.lower() not in (".html", ".htm"):
            html_path = html_path.with_suffix(".html")
        try:
            html_path.write_text(render_html(self._result), encoding="utf-8")
            csv_path = html_path.with_suffix(".csv")
            csv_path.write_text(trades_to_csv(self._result), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        QMessageBox.information(
            self, "已保存", f"报告：{html_path}\n明细：{csv_path}"
        )

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        self._on_stop()
        runner = self._runner
        if runner is not None and runner.isRunning():
            runner.wait(3000)
        super().closeEvent(event)
