"""Self-drawn candlestick item for pyqtgraph."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pyqtgraph as pg
from PyQt6.QtCore import QLineF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPicture

if TYPE_CHECKING:
    from pa_agent.data.base import KlineBar

# Candle colors — brighter for better contrast on dark backgrounds
# close >= open → price went UP
# close <  open → price went DOWN
_GREEN = QColor(0, 208, 132)    # #00d084  vivid green
_RED = QColor(255, 71, 87)      # #ff4757  vivid red

#: 配色方案 → (阳线颜色, 阴线颜色)
_SCHEMES: dict[str, tuple[QColor, QColor]] = {
    # 国际习惯：绿涨红跌
    "intl": (_GREEN, _RED),
    # A 股习惯：红涨绿跌
    "cn": (_RED, _GREEN),
}
_DEFAULT_SCHEME = "cn"
_scheme = _DEFAULT_SCHEME


def normalize_candle_scheme(value: object) -> str:
    """把配置值归一成 ``"cn"`` 或 ``"intl"``（无法识别时按 A 股习惯）。"""
    text = str(value or "").strip().lower()
    if text in ("intl", "international", "global", "us", "west"):
        return "intl"
    if text in ("cn", "china", "a", "ashare", "a_share", "red_up"):
        return "cn"
    return _DEFAULT_SCHEME


def set_candle_scheme(scheme: object) -> bool:
    """切换涨跌配色；返回是否真的发生了变化（调用方据此决定是否重绘）。"""
    global _scheme
    resolved = normalize_candle_scheme(scheme)
    if resolved == _scheme:
        return False
    _scheme = resolved
    return True


def get_candle_scheme() -> str:
    """当前生效的配色方案名。"""
    return _scheme


def candle_colors() -> tuple[QColor, QColor]:
    """返回当前方案的 (阳线颜色, 阴线颜色) 副本。"""
    up, down = _SCHEMES[_scheme]
    return QColor(up), QColor(down)

# Candle body width as a fraction of the x-spacing (0..1)
_BODY_WIDTH = 0.68
_FORMING_BODY_WIDTH = 0.58


class CandleItem(pg.GraphicsObject):
    """A single OHLCV candlestick drawn via QPainter.

    Parameters
    ----------
    bar:
        The KlineBar data for this candle.
    x_pos:
        Integer x-axis position (0 = leftmost / oldest visible candle).
    forming:
        When True, draw the unclosed bar as a hollow ghost candle (live chart only).
    """

    def __init__(self, bar: "KlineBar", x_pos: int, *, forming: bool = False) -> None:
        super().__init__()
        self._bar = bar
        self._x = x_pos
        self._forming = forming
        self._generate_picture()

    # ── pyqtgraph interface ───────────────────────────────────────────────────

    def boundingRect(self) -> QRectF:
        half = (_FORMING_BODY_WIDTH if self._forming else _BODY_WIDTH) / 2.0
        top = self._bar.high
        bottom = self._bar.low
        span = top - bottom
        margin = span * 0.05 + 1e-8
        return QRectF(
            self._x - half,
            bottom - margin,
            _FORMING_BODY_WIDTH if self._forming else _BODY_WIDTH,
            span + 2 * margin,
        )

    def paint(
        self,
        painter: QPainter,
        option: object,  # QStyleOptionGraphicsItem
        widget: object = None,
    ) -> None:
        painter.drawPicture(0, 0, self._picture)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _generate_picture(self) -> None:
        """Pre-render the candle into a QPicture for fast repaints."""
        self._picture = QPicture()
        p = QPainter(self._picture)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        bar = self._bar
        x = float(self._x)
        if self._forming:
            self._paint_forming(p, bar, x)
        else:
            self._paint_closed(p, bar, x)

        p.end()

    def _paint_closed(self, p: QPainter, bar: "KlineBar", x: float) -> None:
        up_color, down_color = candle_colors()
        color = up_color if bar.close >= bar.open else down_color
        p.setPen(QPen(color, 0))
        p.setBrush(color)
        half = _BODY_WIDTH / 2.0
        body_top, body_bottom = self._body_bounds(bar)
        body_rect = QRectF(x - half, body_bottom, _BODY_WIDTH, body_top - body_bottom)
        p.drawRect(body_rect)
        self._paint_wicks(p, bar, x, body_top, body_bottom, QPen(color, 0))

    def _paint_forming(self, p: QPainter, bar: "KlineBar", x: float) -> None:
        up_color, down_color = candle_colors()
        base = up_color if bar.close >= bar.open else down_color
        outline = QColor(base.red(), base.green(), base.blue(), 255)
        fill = QColor(base.red(), base.green(), base.blue(), 70)
        wick_pen = QPen(outline, 1)
        wick_pen.setCosmetic(True)
        # Thicker dashed border for forming bar
        border_pen = QPen(outline, 2)
        border_pen.setCosmetic(True)
        border_pen.setStyle(Qt.PenStyle.DashLine)

        half = _FORMING_BODY_WIDTH / 2.0
        body_top, body_bottom = self._body_bounds(bar)
        span = bar.high - bar.low
        min_body = max(span * 0.06, 1e-6) if span > 0 else 1e-6
        if body_top - body_bottom < min_body:
            mid = (body_top + body_bottom) / 2.0
            body_top = mid + min_body / 2.0
            body_bottom = mid - min_body / 2.0
        body_rect = QRectF(x - half, body_bottom, _FORMING_BODY_WIDTH, body_top - body_bottom)

        self._paint_wicks(p, bar, x, body_top, body_bottom, wick_pen)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRect(body_rect)

        p.setPen(border_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(body_rect)

    @staticmethod
    def _body_bounds(bar: "KlineBar") -> tuple[float, float]:
        body_top = max(bar.open, bar.close)
        body_bottom = min(bar.open, bar.close)
        body_height = body_top - body_bottom
        if body_height < 1e-8:
            mid = (bar.open + bar.close) / 2.0
            body_top = mid + 1e-8
            body_bottom = mid - 1e-8
        return body_top, body_bottom

    @staticmethod
    def _paint_wicks(
        p: QPainter,
        bar: "KlineBar",
        x: float,
        body_top: float,
        body_bottom: float,
        pen: QPen,
    ) -> None:
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if bar.high > body_top:
            p.drawLine(QLineF(x, body_top, x, bar.high))
        if bar.low < body_bottom:
            p.drawLine(QLineF(x, body_bottom, x, bar.low))
