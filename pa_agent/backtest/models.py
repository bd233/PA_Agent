"""Data structures for the walk-forward backtest."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DIR_LONG = "做多"
DIR_SHORT = "做空"
ORDER_MARKET = "市价单"
ORDER_LIMIT = "限价单"
ORDER_STOP = "突破单"

#: AI 计划里表示「不给单」的取值
NO_ORDER_VALUES = frozenset({"", "不下单", "观望", "none", "None", "null"})


@dataclass(frozen=True)
class BacktestConfig:
    """回测参数（全部来自界面，默认值即界面默认值）。"""

    symbol: str
    timeframe: str
    bar_count: int = 30
    initial_capital: float = 1_000_000.0
    position_pct: float = 1.0
    analysis_bars: int = 100
    lot_size: int = 100
    commission_rate: float = 0.0003
    stamp_duty_rate: float = 0.001
    slippage_rate: float = 0.0
    entry_valid_bars: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "标的": self.symbol,
            "周期": self.timeframe,
            "回测K线数": self.bar_count,
            "初始资金": self.initial_capital,
            "仓位比例": f"{self.position_pct:.0%}",
            "喂给AI的K线数": self.analysis_bars,
            "每手股数": self.lot_size,
            "佣金率": f"{self.commission_rate:.4%}",
            "印花税率(卖出)": f"{self.stamp_duty_rate:.4%}",
            "滑点": f"{self.slippage_rate:.4%}",
            "挂单有效K线数": self.entry_valid_bars,
        }


@dataclass
class TradePlan:
    """AI 给出的交易计划（as-of 某一根 K 线）。"""

    ts_open: int
    direction: str
    order_type: str
    entry: float | None
    stop: float | None
    tp: float | None
    tp2: float | None
    win_rate: float | None = None
    cycle: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        if self.direction not in (DIR_LONG, DIR_SHORT):
            return False
        if self.order_type in NO_ORDER_VALUES:
            return False
        if self.entry is None or self.stop is None:
            return False
        if self.direction == DIR_LONG and self.stop >= self.entry:
            return False
        if self.direction == DIR_SHORT and self.stop <= self.entry:
            return False
        return self.entry > 0


@dataclass
class SimTrade:
    """一笔已完成（或被强平）的模拟交易。"""

    plan_ts: int
    entry_ts: int
    exit_ts: int
    direction: str
    order_type: str
    entry: float
    exit: float
    stop: float
    tp: float | None
    tp2: float | None
    qty: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    equity_after: float
    hit_tp2: bool = False
    bars_held: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "计划时间": fmt_ts(self.plan_ts),
            "成交时间": fmt_ts(self.entry_ts),
            "平仓时间": fmt_ts(self.exit_ts),
            "方向": self.direction,
            "挂单类型": self.order_type,
            "入场价": round(self.entry, 4),
            "出场价": round(self.exit, 4),
            "止损": round(self.stop, 4),
            "止盈": None if self.tp is None else round(self.tp, 4),
            "数量": self.qty,
            "盈亏": round(self.pnl, 2),
            "收益率": f"{self.pnl_pct:.2%}",
            "持有K线": self.bars_held,
            "平仓原因": self.exit_reason,
            "触及TP2": "是" if self.hit_tp2 else "否",
            "账户余额": round(self.equity_after, 2),
        }


@dataclass
class StepLog:
    """一次 as-of 分析的记录（含未下单的步骤）。"""

    ts_open: int
    order_type: str
    direction: str
    entry: float | None
    note: str
    traded: bool


@dataclass
class BacktestResult:
    config: BacktestConfig
    steps: list[StepLog] = field(default_factory=list)
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    skipped: int = 0
    failures: int = 0
    cancelled: bool = False
    error: str = ""
    started_iso: str = ""
    finished_iso: str = ""
    #: 回测窗口用的 K 线（newest-first），供图表叠加成交点位；元素为 KlineBar
    bars: tuple = ()

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.config.initial_capital

    def summary(self) -> dict[str, Any]:
        cfg = self.config
        trades = self.trades
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        peak = cfg.initial_capital
        max_dd = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
        total_return = (self.final_equity - cfg.initial_capital) / cfg.initial_capital
        analyzed = len(self.steps)
        return {
            "最终权益": self.final_equity,
            "总盈亏": self.final_equity - cfg.initial_capital,
            "总收益率": total_return,
            "分析次数": analyzed,
            "下单次数": len(trades),
            "胜/负": f"{len(wins)} / {len(losses)}",
            "胜率": (len(wins) / len(trades)) if trades else 0.0,
            "平均盈利": (gross_win / len(wins)) if wins else 0.0,
            "平均亏损": (gross_loss / len(losses)) if losses else 0.0,
            "盈亏比": (gross_win / gross_loss) if gross_loss else None,
            "最大回撤": max_dd,
            "触及TP2": sum(1 for t in trades if t.hit_tp2),
            "跳过(历史不足)": self.skipped,
            "失败(分析异常)": self.failures,
            "已取消": self.cancelled,
        }


def fmt_ts(ts_ms: int | None) -> str:
    """毫秒时间戳 → 本地 `YYYY-MM-DD HH:MM`（缺失返回 `-`）。"""
    if not ts_ms:
        return "-"
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"
