"""京东金融「银行积存金」数据源：浙商 / 民生 / 工行 / 光大 / 兴业 / 中信。

接口（均为 ``https://api.jdjygold.com`` 上的 POST JSON，移动端 UA 即可，无需登录）：

- 日 K：``/gw2/generic/hj/h5/m/cfGetQuotesPriceKLine``
  入参 ``{"productSku": "...", "periodType": "m1" | "m6" | "y1"}``
  返回 ``resultData.data.line = [{"date": "20261010", "price": "905.46", "raisePercent": "0.0000"}, ...]``
  ``m1`` ≈ 28 点、``m6`` ≈ 150 点、``y1`` ≈ 288 点（最多约一年）。
- 当日分时：``/gw2/generic/hj/h5/m/cfGetPriceTrendChart``
  入参 ``{"appChannel": "11", "beginTime": "", "priceType": "buy", "productSku": "..."}``
  返回 ``resultData.data.dataList = [{"goldPriceTime": "2026-11-11 00:00:00", "goldPrice": "868.74"}, ...]``
  实测只有**当天**（``beginTime`` 被服务端忽略）。
- 最新一口价：``/gw2/generic/hj/h5/m/cfGetLatestPriceInfo``（见 skill 脚本，本模块未用）。

⚠️ 限制：
- 银行不发布成交量，``volume`` 恒为 0，量价类判断不可用；
- 日线接口**只有每日一个价格**（无开高低）。本模块把前一日收盘价当作当日开盘价，
  实体高度 = 当日涨跌幅；日内真实振幅／影线无从得知，K 线只能看「方向与幅度」；
- 分钟线只有当日，隔日失效，无法回溯历史分钟；
- 价格是银行自己的买卖报价（含点差），与上金所 Au99.99 存在 1~3 元/克的常态价差，
  做积存金请以本源的报价为准。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar, normalize_kline_bar
from pa_agent.data.sge_source import _resample_rows

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

_API_BASE = "https://api.jdjygold.com/gw2/generic"
_KLINE_PATH = "/hj/h5/m/cfGetQuotesPriceKLine"
_TREND_PATH = "/hj/h5/m/cfGetPriceTrendChart"

#: 银行 -> 京东金融 productSku
JD_GOLD_PRODUCTS: dict[str, str] = {
    "浙商积存金": "1961543816",
    "民生积存金": "21001001000001",
    "工行积存金": "2005453243",
    "光大积存金": "2024345112",
    "兴业积存金": "2039007297",
    "中信积存金": "2045976593",
}

JD_GOLD_DEFAULT_SYMBOL = "浙商积存金"

_ALIASES: dict[str, str] = {
    "浙商": "浙商积存金",
    "CZB": "浙商积存金",
    "民生": "民生积存金",
    "CMBC": "民生积存金",
    "工行": "工行积存金",
    "工商": "工行积存金",
    "ICBC": "工行积存金",
    "光大": "光大积存金",
    "CGB": "光大积存金",
    "兴业": "兴业积存金",
    "CIB": "兴业积存金",
    "中信": "中信积存金",
    "CNCB": "中信积存金",
}

_SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1d", "5m", "1m")
_TF_MINUTES: dict[str, int] = {"5m": 5, "1m": 1}

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def normalize_jd_gold_symbol(symbol: str | None) -> str:
    """把用户输入归一到 ``JD_GOLD_PRODUCTS`` 的键；无法识别返回空串。"""
    raw = (symbol or "").strip()
    if not raw:
        return ""
    tokens = [t for t in raw.replace("（", " ").replace("）", " ").replace("(", " ").replace(")", " ").split() if t]
    if not tokens:
        return ""
    first = tokens[0]
    if first in JD_GOLD_PRODUCTS:
        return first
    upper = first.upper()
    if upper in _ALIASES:
        return _ALIASES[upper]
    # 允许直接粘 productSku
    for name, sku in JD_GOLD_PRODUCTS.items():
        if first == sku:
            return name
    for name in JD_GOLD_PRODUCTS:
        if first in name:
            return name
    return ""


def _post_json(path: str, body: dict[str, Any], timeout: float = 20.0) -> Any:
    url = _API_BASE + path
    headers = {
        "User-Agent": _UA,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        from curl_cffi import requests as curl_requests  # type: ignore

        resp = curl_requests.post(
            url, json=body, headers=headers, impersonate="chrome", timeout=timeout
        )
    except ImportError:  # pragma: no cover
        import requests

        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise DataSourceTransientError(f"京东金融接口返回 HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise DataSourceTransientError("京东金融接口返回非 JSON") from exc
    if not payload.get("success", True):
        raise DataSourceTransientError(
            f"京东金融接口报错: {payload.get('resultMsg') or payload.get('resultCode')}"
        )
    return payload


def _parse_kline_line(line: list[Any]) -> list[dict[str, Any]]:
    """``[{"date":"20261010","price":"905.46"}, ...]`` → 升序行。

    银行日线只给一个价格（当日收盘），故把**前一日收盘价当作当日开盘价**，
    影线取两者高低 —— 实体高度 = 当日涨跌幅；日内真实振幅无从得知（接口没给）。
    """
    rows: list[dict[str, Any]] = []
    parsed: list[tuple[int, float]] = []
    for item in line:
        if not isinstance(item, dict):
            continue
        date_text = str(item.get("date") or "").strip()
        try:
            day = datetime.strptime(date_text, "%Y%m%d").replace(tzinfo=_CN_TZ)
            price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        parsed.append((int(day.timestamp() * 1000), price))
    parsed.sort(key=lambda pair: pair[0])

    prev_close: float | None = None
    for ts, price in parsed:
        open_price = prev_close if prev_close is not None else price
        rows.append(
            {
                "ts_open": ts,
                "open": open_price,
                "high": max(open_price, price),
                "low": min(open_price, price),
                "close": price,
                "volume": 0.0,
            }
        )
        prev_close = price
    return rows


def _parse_trend_points(items: list[Any]) -> list[dict[str, Any]]:
    """``[{"goldPriceTime":"2026-11-11 00:00:00","goldPrice":"868.74"}, ...]`` → 1 分钟行。

    分时只有每分钟一个价：开 = 上一分钟收盘，高低包住两者。
    """
    ticks: list[tuple[int, float]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("goldPriceTime") or "").strip()
        try:
            ts = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CN_TZ)
            price = float(item.get("goldPrice"))
        except (TypeError, ValueError):
            continue
        ticks.append((int(ts.timestamp() * 1000), price))
    ticks.sort(key=lambda pair: pair[0])

    rows: list[dict[str, Any]] = []
    prev_close: float | None = None
    for ms, price in ticks:
        open_price = prev_close if prev_close is not None else price
        rows.append(
            {
                "ts_open": ms,
                "open": open_price,
                "high": max(open_price, price),
                "low": min(open_price, price),
                "close": price,
                "volume": 0.0,
            }
        )
        prev_close = price
    return rows


class JdGoldSource(DataSource):
    """银行积存金（京东金融报价），日线最多约一年 + 当日分时。"""

    HAS_VOLUME = False

    def __init__(self) -> None:
        self._symbol: str = ""
        self._sku: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._warned_no_volume: bool = False

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._connected = True
        logger.info("JdGoldSource connected (京东金融银行积存金报价)")

    def disconnect(self) -> None:
        self._connected = False
        self._symbol = ""
        self._sku = ""
        self._timeframe = ""
        logger.info("JdGoldSource disconnected")

    # ── 元数据 ──────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return [f"{name} {sku}" for name, sku in JD_GOLD_PRODUCTS.items()]

    def supported_timeframes(self) -> list[str]:
        return list(_SUPPORTED_TIMEFRAMES)

    def is_symbol_available(self, symbol: str) -> bool:
        return bool(normalize_jd_gold_symbol(symbol))

    # ── 订阅 ────────────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"不支持的周期: {timeframe!r}. 银行积存金请使用 {list(_SUPPORTED_TIMEFRAMES)} 之一"
                "（分钟周期仅有当日数据）"
            )
        name = normalize_jd_gold_symbol(symbol)
        if not name:
            raise ValueError(
                "品种无效: 请输入 浙商积存金 / 民生积存金 / 工行积存金 / 光大积存金 / 兴业积存金 / 中信积存金"
            )
        self._symbol = name
        self._sku = JD_GOLD_PRODUCTS[name]
        self._timeframe = timeframe
        logger.info("JdGoldSource subscribed: %s (%s) %s", name, self._sku, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._sku = ""
        self._timeframe = ""
        logger.info("JdGoldSource unsubscribed")

    # ── 快照 ────────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("银行积存金数据源未连接")
        if not self._sku or not self._timeframe:
            raise DataSourceTransientError("未订阅积存金品种/周期")

        if not self._warned_no_volume:
            logger.warning(
                "银行积存金不发布成交量：%s 的 volume 恒为 0，量价类判断不可用", self._symbol
            )
            self._warned_no_volume = True

        try:
            if self._timeframe == "1d":
                rows_asc = self._fetch_daily(n)
            else:
                rows_asc = self._fetch_intraday(self._timeframe)
        except DataSourceTransientError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("银行积存金拉取失败: %s", exc)
            raise DataSourceTransientError(f"银行积存金拉取失败: {exc}") from exc

        if not rows_asc:
            raise DataSourceTransientError(f"未返回积存金数据: {self._symbol} {self._timeframe}")

        rows_asc = rows_asc[-max(n + 5, 30):]
        rows_newest = list(reversed(rows_asc))
        intraday = self._timeframe != "1d"
        for i, row in enumerate(rows_newest):
            row["closed"] = not (i == 0 and intraday)

        return self._to_bars(rows_newest, n)

    # ── 抓取 ────────────────────────────────────────────────────────────────

    def _fetch_daily(self, n: int) -> list[dict[str, Any]]:
        period = "y1" if n > 150 else ("m6" if n > 30 else "m1")
        payload = _post_json(_KLINE_PATH, {"productSku": self._sku, "periodType": period})
        data = ((payload.get("resultData") or {}).get("data") or {})
        line = data.get("line") or []
        return _parse_kline_line(line)

    def _fetch_intraday(self, timeframe: str) -> list[dict[str, Any]]:
        payload = _post_json(
            _TREND_PATH,
            {
                "appChannel": "11",
                "beginTime": "",
                "priceType": "buy",
                "productSku": self._sku,
            },
        )
        data = ((payload.get("resultData") or {}).get("data") or {})
        minute_rows = _parse_trend_points(data.get("dataList") or [])
        return _resample_rows(minute_rows, _TF_MINUTES.get(timeframe, 1))

    # ── 转换 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _to_bars(rows_newest_first: list[dict[str, Any]], n: int) -> list[KlineBar]:
        bars: list[KlineBar] = []
        for i, row in enumerate(rows_newest_first[:n]):
            bars.append(
                normalize_kline_bar(
                    KlineBar(
                        seq=i + 1,
                        ts_open=float(int(row["ts_open"])),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume", 0.0) or 0.0),
                        closed=bool(row.get("closed", i != 0)),
                    )
                )
            )
        return bars
