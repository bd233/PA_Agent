"""把回测成交映射成图表坐标（纯计算，不依赖 Qt，便于单测）。

图表横轴口径与 ``ChartWidget._render_frame`` 一致：``bars`` 为 newest-first，
最老一根在 ``x=0``、最新一根在 ``x=n-1``，即 ``x = n - 1 - i``。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "TradeLine",
    "TradeMarker",
    "TradeOverlay",
    "TradeSpan",
    "build_trade_overlay",
    "x_for_ts",
]


@dataclass(frozen=True)
class TradeMarker:
    """一个成交点（入场或出场）。"""

    x: float
    y: float
    kind: str  # "entry" | "exit"
    direction: str  # 做多 / 做空
    label: str
    pnl: float = 0.0


@dataclass(frozen=True)
class TradeLine:
    """一段横线/斜线：止损、止盈、或入场→出场的连线。"""

    x0: float
    y0: float
    x1: float
    y1: float
    kind: str  # "stop" | "target" | "target2" | "fill"


@dataclass(frozen=True)
class TradeSpan:
    """背景区间：``hold``=持仓段，``window``=本次回测覆盖的窗口。"""

    x0: float
    x1: float
    kind: str = "hold"


@dataclass(frozen=True)
class TradeOverlay:
    markers: tuple[TradeMarker, ...] = ()
    lines: tuple[TradeLine, ...] = ()
    spans: tuple[TradeSpan, ...] = ()
    price_bounds: tuple[float, float] | None = None

    @property
    def is_empty(self) -> bool:
        return not self.markers and not self.spans


def x_for_ts(ts: int, ts_asc: Sequence[int]) -> float:
    """把 K 线时间戳换成图表 x 坐标；找不到时退到最近的左侧/右侧。"""
    if not ts_asc:
        return 0.0
    target = int(ts)
    idx = bisect.bisect_left(ts_asc, target)
    if idx >= len(ts_asc):
        return float(len(ts_asc) - 1)
    if ts_asc[idx] == target:
        return float(idx)
    # 落在两根之间：取更近的一根
    if idx == 0:
        return 0.0
    left_gap = target - ts_asc[idx - 1]
    right_gap = ts_asc[idx] - target
    return float(idx - 1 if left_gap <= right_gap else idx)


def build_trade_overlay(
    bars_newest_first: Sequence[Any],
    trades: Sequence[Any],
    *,
    window_bars: int = 0,
) -> TradeOverlay:
    """把 ``SimTrade`` 列表映射到蜡烛图上的点、线段与持仓区间。

    * ``bars_newest_first`` 中的元素需有 ``ts_open`` 属性（或同名键）。
    * 没有成交时返回空 overlay（调用方仍可只画蜡烛）。
    """
    if not bars_newest_first:
        return TradeOverlay()

    n_bars = len(bars_newest_first)
    spans: list[TradeSpan] = []
    if window_bars and window_bars > 0:
        start = max(0, n_bars - int(window_bars))
        spans.append(TradeSpan(start - 0.5, n_bars - 0.5, kind="window"))

    if not trades:
        return TradeOverlay(spans=tuple(spans))

    ts_asc: list[int] = []
    for bar in reversed(list(bars_newest_first)):
        ts_asc.append(int(_attr(bar, "ts_open")))

    markers: list[TradeMarker] = []
    lines: list[TradeLine] = []
    prices: list[float] = []

    for trade in trades:
        direction = str(_attr(trade, "direction", "") or "")
        entry = float(_attr(trade, "entry", 0.0) or 0.0)
        exit_price = float(_attr(trade, "exit", entry) or entry)
        stop = _opt_float(_attr(trade, "stop", None))
        tp = _opt_float(_attr(trade, "tp", None))
        tp2 = _opt_float(_attr(trade, "tp2", None))
        pnl = float(_attr(trade, "pnl", 0.0) or 0.0)

        x_entry = x_for_ts(int(_attr(trade, "entry_ts", 0) or 0), ts_asc)
        x_exit = x_for_ts(int(_attr(trade, "exit_ts", 0) or 0), ts_asc)
        if x_exit < x_entry:  # 防御：同一根成交
            x_exit = x_entry

        is_long = direction != "做空"
        markers.append(
            TradeMarker(
                x=x_entry,
                y=entry,
                kind="entry",
                direction=direction,
                label="买" if is_long else "卖",
                pnl=pnl,
            )
        )
        markers.append(
            TradeMarker(
                x=x_exit,
                y=exit_price,
                kind="exit",
                direction=direction,
                label="平",
                pnl=pnl,
            )
        )
        lines.append(TradeLine(x_entry, entry, x_exit, exit_price, "fill"))
        if stop is not None:
            lines.append(TradeLine(x_entry, stop, x_exit, stop, "stop"))
            prices.append(stop)
        if tp is not None:
            lines.append(TradeLine(x_entry, tp, x_exit, tp, "target"))
            prices.append(tp)
        if tp2 is not None:
            lines.append(TradeLine(x_entry, tp2, x_exit, tp2, "target2"))
            prices.append(tp2)
        spans.append(TradeSpan(x_entry, x_exit, kind="hold"))
        prices.extend([entry, exit_price])

    low, high = min(prices), max(prices)
    return TradeOverlay(
        markers=tuple(markers),
        lines=tuple(lines),
        spans=tuple(spans),
        price_bounds=(low, high) if high > low else None,
    )


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
