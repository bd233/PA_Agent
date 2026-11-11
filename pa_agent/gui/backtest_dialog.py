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
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
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
        self.resize(1240, 880)
        self.setModal(True)

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(14)

        # ── 左栏：参数 / 提示 / 进度 / 按钮 / 日志 ─────────────────────────────
        left_col = QVBoxLayout()
        left_col.setSpacing(8)

        form_box = QGroupBox("回测参数")
        grid = QGridLayout(form_box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        def label(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return lab

        self._symbol_edit = QLineEdit(symbol)
        self._symbol_edit.setPlaceholderText("留空则用当前标的")
        grid.addWidget(label("标的"), 0, 0)
        grid.addWidget(self._symbol_edit, 0, 1, 1, 3)

        self._tf_combo = QComboBox()
        for tf in self._timeframe_choices(timeframe):
            self._tf_combo.addItem(tf)
        if timeframe and self._tf_combo.findText(timeframe) < 0:
            self._tf_combo.addItem(timeframe)
            self._tf_combo.setCurrentText(timeframe)
        grid.addWidget(label("周期"), 1, 0)
        grid.addWidget(self._tf_combo, 1, 1)

        self._bars_spin = QSpinBox()
        self._bars_spin.setRange(2, 2000)
        self._bars_spin.setValue(30)
        self._bars_spin.setSingleStep(5)
        self._bars_spin.setToolTip("回测最近 N 根已收盘 K 线")
        self._bars_spin.valueChanged.connect(self._update_estimate)
        grid.addWidget(label("K线数 N"), 1, 2)
        grid.addWidget(self._bars_spin, 1, 3)

        self._capital_spin = QDoubleSpinBox()
        self._capital_spin.setRange(1000.0, 1e12)
        self._capital_spin.setDecimals(0)
        self._capital_spin.setSingleStep(100_000.0)
        self._capital_spin.setValue(1_000_000.0)
        self._capital_spin.setGroupSeparatorShown(True)
        grid.addWidget(label("初始资金"), 2, 0)
        grid.addWidget(self._capital_spin, 2, 1)

        self._position_spin = QSpinBox()
        self._position_spin.setRange(1, 100)
        self._position_spin.setValue(100)
        self._position_spin.setSuffix(" %")
        self._position_spin.setToolTip("每次下单使用当前权益的比例")
        grid.addWidget(label("单笔仓位"), 2, 2)
        grid.addWidget(self._position_spin, 2, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        left_col.addWidget(form_box)

        self._adv_toggle = QPushButton("高级参数：费用与撮合 ▸")
        self._adv_toggle.setCheckable(True)
        self._adv_toggle.setChecked(False)
        self._adv_toggle.toggled.connect(self._on_toggle_adv)
        left_col.addWidget(self._adv_toggle)

        adv_box = QWidget()
        adv_form = QFormLayout(adv_box)
        adv_form.setContentsMargins(10, 2, 0, 2)
        adv_form.setVerticalSpacing(6)
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
        adv_box.setVisible(False)
        self._adv_box = adv_box
        left_col.addWidget(adv_box)

        self._hint = QLabel()
        self._hint.setStyleSheet("color:#8b95a7;")
        self._hint.setWordWrap(True)
        left_col.addWidget(self._hint)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        left_col.addWidget(self._progress)

        btn_grid = QGridLayout()
        btn_grid.setHorizontalSpacing(8)
        btn_grid.setVerticalSpacing(6)
        self._start_btn = QPushButton("开始回测")
        self._start_btn.clicked.connect(self._on_start)
        btn_grid.addWidget(self._start_btn, 0, 0)
        self._stop_btn = QPushButton("停止")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_grid.addWidget(self._stop_btn, 0, 1)
        self._save_btn = QPushButton("保存报告")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        btn_grid.addWidget(self._save_btn, 1, 0)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.reject)
        btn_grid.addWidget(self._close_btn, 1, 1)
        left_col.addLayout(btn_grid)

        self._summary = QLabel("尚未运行。")
        self._summary.setWordWrap(True)
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left_col.addWidget(self._summary)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("进度日志…")
        left_col.addWidget(self._log, 1)

        left_wrap = QWidget()
        left_wrap.setLayout(left_col)
        left_wrap.setFixedWidth(430)
        root.addWidget(left_wrap)

        # ── 右栏：K 线（含成交点位）+ 明细表 ──────────────────────────────────
        from pa_agent.gui.chart_widget import ChartWidget

        self._chart = ChartWidget()
        self._chart.setMinimumHeight(320)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["计划时间", "方向/挂单", "入场", "出场", "持有", "原因", "盈亏", "余额"]
        )
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._chart)
        split.addWidget(self._table)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([560, 240])

        right_col = QVBoxLayout()
        right_col.setSpacing(8)
        right_col.addWidget(split, 1)
        root.addLayout(right_col, 1)

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

    def _on_toggle_adv(self, on: bool) -> None:
        self._adv_box.setVisible(on)
        self._adv_toggle.setText("高级参数：费用与撮合 ▾" if on else "高级参数：费用与撮合 ▸")

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

        # 用独立的源实例按「对话框里的标的 + 周期」重新订阅：
        # 主界面当前品种/周期可能与回测参数不同，直接复用会喂错 K 线。
        run_source, err = self._make_run_source(cfg.symbol, cfg.timeframe)
        if run_source is None:
            QMessageBox.warning(self, "数据源不可用", err or "无法按该标的/周期取数。")
            return
        self._run_source = run_source

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
        self._chart.clear_backtest_overlay()
        self._summary.setText("运行中…")
        self._log.clear()
        self._progress.setRange(0, cfg.bar_count)
        self._progress.setValue(0)
        self._save_btn.setEnabled(False)
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)

        runner = BacktestRunner(
            config=cfg,
            fetch_bars=lambda need: run_source.latest_snapshot(need),
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
        self._render_chart(result)

    def _render_chart(self, result: BacktestResult) -> None:
        """把回测窗口的 K 线画出来，并把每笔成交标到蜡烛上。"""
        from pa_agent.backtest.chartmap import build_trade_overlay
        from pa_agent.data.snapshot import build_analysis_frame

        bars = list(getattr(result, "bars", ()) or ())
        if not bars:
            return
        cfg = result.config
        frame = build_analysis_frame(bars, len(bars), cfg.symbol, cfg.timeframe)
        if frame is None:
            self._append_log("⚠ K 线不足，图表未绘制")
            return
        self._chart.set_frame_now(frame, fit_view=True)
        overlay = build_trade_overlay(
            bars, result.trades, window_bars=int(getattr(result, "window_bars", 0) or 0)
        )
        self._chart.set_backtest_overlay(overlay)
        if not result.trades:
            self._append_log("（本次没有成交，图上只有 K 线与回测区间底色）")

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
        self._release_run_source()

    def _make_run_source(self, symbol: str, timeframe: str) -> tuple[Any, str]:
        """按对话框参数新建一个数据源并订阅，取数后与主界面互不影响。"""
        kind = self._current_kind()
        try:
            from pa_agent.data.factory import create_data_source

            source = create_data_source(kind)
            source.connect()
            source.subscribe(symbol, timeframe)
        except Exception as exc:  # noqa: BLE001 - 交给调用方提示
            logger.warning("backtest source init failed: %s", exc)
            return None, f"{symbol} / {timeframe} 取数失败：{exc}"
        return source, ""

    def _current_kind(self) -> str:
        settings = getattr(self._ctx, "settings", None)
        general = getattr(settings, "general", None) if settings is not None else None
        kind = getattr(general, "last_data_source", None)
        if kind:
            return str(kind)
        source = getattr(self._ctx, "data_source", None)
        mapping = {
            "EastMoneySource": "ashare",
            "GoldSource": "gold",
            "TradingViewSource": "tradingview",
            "EastMoneyFuturesSource": "eastmoney_futures",
        }
        return mapping.get(type(source).__name__ if source is not None else "", "ashare")

    def _release_run_source(self) -> None:
        source = getattr(self, "_run_source", None)
        self._run_source = None
        if source is None:
            return
        try:
            source.disconnect()
        except Exception as exc:  # noqa: BLE001 - 释放失败不影响界面
            logger.debug("backtest source disconnect failed: %s", exc)

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
            html_path.write_text(
                render_html(self._result, chart_png=self._chart_png()), encoding="utf-8"
            )
            csv_path = html_path.with_suffix(".csv")
            csv_path.write_text(trades_to_csv(self._result), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        QMessageBox.information(
            self, "已保存", f"报告：{html_path}\n明细：{csv_path}"
        )

    def _chart_png(self) -> bytes | None:
        """把当前图表抓成 PNG，嵌进报告（失败则报告只留数据表）。"""
        try:
            from PyQt6.QtCore import QBuffer, QByteArray, QIODevice

            pixmap = self._chart.grab()
            if pixmap.isNull():
                return None
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            pixmap.save(buffer, "PNG")
            buffer.close()
            return bytes(data.data()) if data.size() > 0 else None
        except Exception as exc:  # noqa: BLE001 - 报告不能因截图失败而保存不了
            logger.debug("chart grab failed: %s", exc)
            return None

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        self._on_stop()
        runner = self._runner
        if runner is not None and runner.isRunning():
            runner.wait(3000)
        super().closeEvent(event)
