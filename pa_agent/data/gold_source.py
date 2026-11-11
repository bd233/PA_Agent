"""统一「黄金」数据源：品种下拉里选 黄金9999（上金所现货）或 浙商积存金（京东金融报价）。

设计要点
--------
* 界面只有一个「黄金」入口，具体市场由 **品种** 决定，避免数据来源下拉框里堆一长串黄金条目。
* 这里不复制行情逻辑，只按品种把请求转给 :class:`SgeSource` / :class:`JdGoldSource`，
  两者共享同一套周期约定（``1d`` / ``5m`` / ``1m``）与「无成交量」事实。
* 两个底层源的历史与分时口径不同（日线根数、是否有当日累计量），文档里已写明。
"""
from __future__ import annotations

from typing import Any

from pa_agent.data.base import DataSource, KlineBar

#: 界面下拉里展示的品种名（也是 subscribe 接受的写法）。
GOLD_INSTRUMENTS: tuple[str, ...] = ("黄金9999", "浙商积存金")

#: 默认品种：上金所现货，历史最长。
GOLD_DEFAULT_SYMBOL = "黄金9999"

#: 品种别名 → 规范名（大小写不敏感）。
_GOLD_ALIASES: dict[str, str] = {
    "黄金9999": "黄金9999",
    "AU9999": "黄金9999",
    "AU99.99": "黄金9999",
    "AU99_99": "黄金9999",
    "GOLD9999": "黄金9999",
    "SGE": "黄金9999",
    "上金所": "黄金9999",
    "上金所现货": "黄金9999",
    "XAU": "黄金9999",
    "XAUUSD": "黄金9999",
    "浙商": "浙商积存金",
    "浙商积存金": "浙商积存金",
    "CZB": "浙商积存金",
    "1961543816": "浙商积存金",
}

#: 规范名 → 底层 kind（与 factory 的 kind 对齐）。
_INSTRUMENT_KIND: dict[str, str] = {
    "黄金9999": "sge",
    "浙商积存金": "jd_gold",
}

#: 底层 kind 使用的真实代码。
_INSTRUMENT_CODE: dict[str, str] = {
    "黄金9999": "Au99.99",
    "浙商积存金": "浙商积存金",
}

#: 两个底层源可提供的周期（当日足以凑满 100 根分析窗口的那些）。
GOLD_TIMEFRAMES: tuple[str, ...] = ("1d", "5m", "1m")


def normalize_gold_instrument(symbol: str | None) -> str:
    """把用户输入归一成 :data:`GOLD_INSTRUMENTS` 里的名字；无法识别返回空串。"""
    raw = (symbol or "").strip()
    if not raw:
        return ""
    key = raw.split()[0].upper() if " " in raw else raw.upper()
    if key in _GOLD_ALIASES:
        return _GOLD_ALIASES[key]
    # 「浙商积存金(1961543816)」这类带 sku 的写法
    for name in GOLD_INSTRUMENTS:
        if name in raw:
            return name
    return ""


class GoldSource(DataSource):
    """按品种分发到上金所现货 / 银行积存金，对上层暴露统一接口。"""

    HAS_VOLUME = False

    def __init__(self) -> None:
        self._connected = False
        self._symbol = GOLD_DEFAULT_SYMBOL
        self._delegate: DataSource | None = None
        self._delegate_symbol = ""

    # ── 生命周期 ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        self._connected = True

    def disconnect(self) -> None:
        delegate = self._delegate
        self._delegate = None
        self._connected = False
        if delegate is not None:
            try:
                delegate.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def is_connected(self) -> bool:
        return self._connected

    # ── 元数据 ──────────────────────────────────────────────────────────
    def list_symbols(self) -> list[str]:
        return list(GOLD_INSTRUMENTS)

    def supported_timeframes(self) -> list[str]:
        return list(GOLD_TIMEFRAMES)

    def is_symbol_available(self, symbol: str) -> bool:
        return bool(normalize_gold_instrument(symbol))

    # ── 订阅 ────────────────────────────────────────────────────────────
    def subscribe(self, symbol: str, timeframe: str) -> None:
        name = normalize_gold_instrument(symbol)
        if not name:
            raise ValueError(
                f"黄金品种无效: {symbol!r}；可选 {' / '.join(GOLD_INSTRUMENTS)}"
            )
        if timeframe not in GOLD_TIMEFRAMES:
            raise ValueError(
                f"黄金不支持周期 {timeframe!r}；可选 {', '.join(GOLD_TIMEFRAMES)}"
            )
        code = _INSTRUMENT_CODE[name]
        kind = _INSTRUMENT_KIND[name]
        delegate = self._make_delegate(kind)
        if not getattr(delegate, "_connected", False):
            delegate.connect()
        delegate.subscribe(code, timeframe)
        self._user_symbol = name
        self._symbol = code
        self._delegate = delegate
        self._delegate_symbol = code

    def unsubscribe(self) -> None:
        if self._delegate is not None:
            try:
                self._delegate.unsubscribe()
            except Exception:  # noqa: BLE001
                pass

    def _make_delegate(self, kind: str) -> DataSource:
        if kind == "sge":
            from pa_agent.data.sge_source import SgeSource

            return SgeSource()
        from pa_agent.data.jd_gold_source import JdGoldSource

        return JdGoldSource()

    # ── 取数 ────────────────────────────────────────────────────────────
    def latest_snapshot(self, n: int = 200) -> list[KlineBar]:
        delegate = self._delegate
        if delegate is None:
            raise RuntimeError("未订阅行情：先调用 subscribe(symbol, timeframe)")
        return delegate.latest_snapshot(n)

    # ── 辅助 ────────────────────────────────────────────────────────────
    def current_instrument(self) -> str:
        """当前品种的展示名（黄金9999 / 浙商积存金）。"""
        return getattr(self, "_user_symbol", GOLD_DEFAULT_SYMBOL)

    def describe(self) -> dict[str, Any]:
        return {
            "instrument": self.current_instrument(),
            "code": self._symbol,
            "delegate": self._delegate_symbol,
            "has_volume": self.HAS_VOLUME,
        }
