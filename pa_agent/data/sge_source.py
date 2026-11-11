"""上海黄金交易所（SGE）黄金现货数据源：Au99.99 等。

数据接口（上金所官方 JSON，无需登录/令牌）：

- 日 K 线: ``https://www.sge.com.cn/graph/Dailyhq?instid=Au99.99``
  返回 ``{"time": [[日期, 开, 收, 低, 高], ...]}``，约 2400 根（2016-12-19 起）。
- 当日分时: ``https://www.sge.com.cn/graph/quotations?instid=Au99.99``
  返回 ``{"times": ["20:00", ...], "data": ["883.42", ...]}``，逐分钟价格。
- 实时: ``https://hq.sinajs.cn/list=gds_AU9999``（需 Referer）。

⚠️ 重要限制：上金所现货**不发布成交量**。历史日线的 ``volume`` 恒为 0，
只有当日 K 线可从新浪实时快照拿到「当日累计成交量」。因此：

- 量价类判断（放量突破、量价背离、量比）在这个源上不可用；
- 分钟周期只有**当日**分时（隔日即失效），无法回溯历史分钟 K 线；
- 1h 仅有约 14 根（不足 20 根分析门槛），故不提供。

用法：``subscribe("Au99.99", "1d")`` → ``latest_snapshot(n)``。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar, normalize_kline_bar
from pa_agent.data.akshare_source import _merge_ohlcv

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

#: 上金所现货/延期品种: instid -> 中文名
SGE_INSTRUMENTS: dict[str, str] = {
    "Au99.99": "黄金9999",
    "Au100g": "黄金100g",
    "Au(T+D)": "黄金延期",
    "Ag(T+D)": "白银延期",
    "Pt99.95": "铂金9995",
}

#: 默认品种（与银行积存金报价基准一致）
SGE_DEFAULT_SYMBOL = "Au99.99"

#: 新浪实时快照代码（仅 Au99.99 已实测；其余品种返回 None 时跳过实时刷价）
_SINA_CODE: dict[str, str] = {
    "Au99.99": "gds_AU9999",
}

_ALIASES: dict[str, str] = {
    "AU9999": "Au99.99",
    "AU99.99": "Au99.99",
    "AU99_99": "Au99.99",
    "黄金9999": "Au99.99",
    "GOLD": "Au99.99",
    "XAU": "Au99.99",
    "XAUUSD": "Au99.99",
    "AU100G": "Au100g",
    "AU(T+D)": "Au(T+D)",
    "AUTD": "Au(T+D)",
    "AG(T+D)": "Ag(T+D)",
    "AGTD": "Ag(T+D)",
    "PT99.95": "Pt99.95",
    "PT9995": "Pt99.95",
}

#: 支持周期：日线 + 当日分钟（1h 只有约 14 根，低于 20 根分析门槛，不提供）
_SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1d", "30m", "15m", "5m", "1m")

_TF_MINUTES: dict[str, int] = {"30m": 30, "15m": 15, "5m": 5, "1m": 1}

_DAILY_URL = "https://www.sge.com.cn/graph/Dailyhq?instid={inst}"
_MINUTE_URL = "https://www.sge.com.cn/graph/quotations?instid={inst}"
_SINA_URL = "https://hq.sinajs.cn/list={code}"

_HEADERS = {
    "Referer": "https://www.sge.com.cn/sjzx/mrhq",
    "Origin": "https://www.sge.com.cn",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}


def normalize_sge_symbol(symbol: str | None) -> str:
    """把用户输入的品种名归一到上金所 instid；无法识别时返回空串。"""
    raw = (symbol or "").strip()
    if not raw:
        return ""
    # "AU0 黄金" / "Au99.99 黄金9999" 这类「代码 + 中文名」写法取前半段
    code = raw.split()[0].strip()
    upper = code.upper()
    if upper in _ALIASES:
        return _ALIASES[upper]
    for inst in SGE_INSTRUMENTS:
        if inst.upper() == upper:
            return inst
    return ""


def _cn_now() -> datetime:
    return datetime.now(_CN_TZ)


def _session_open(now: datetime | None = None) -> bool:
    """上金所现货交易时段：09:00-11:30、13:30-15:30、20:00-次日 02:30。"""
    now = now or _cn_now()
    minutes = now.hour * 60 + now.minute
    morning = 9 * 60 <= minutes < 11 * 60 + 30
    afternoon = 13 * 60 + 30 <= minutes < 15 * 60 + 30
    night = minutes >= 20 * 60 or minutes < 2 * 60 + 30
    return morning or afternoon or night


def _http_get(url: str, *, referer: str | None = None, timeout: float = 20.0) -> str:
    """优先 curl_cffi（带浏览器指纹），缺失时退回 requests。"""
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        from curl_cffi import requests as curl_requests  # type: ignore

        resp = curl_requests.get(url, headers=headers, impersonate="chrome", timeout=timeout)
    except ImportError:  # pragma: no cover - 依赖缺失时的兜底
        import requests

        resp = requests.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise DataSourceTransientError(f"上金所接口返回 HTTP {resp.status_code}")
    return resp.text


def _parse_daily(payload: Any) -> list[dict[str, Any]]:
    """``{"time": [[日期, 开, 收, 低, 高], ...]}`` → 升序行（volume=0）。"""
    rows: list[dict[str, Any]] = []
    items = payload.get("time") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return rows
    for item in items:
        if not isinstance(item, list) or len(item) < 5:
            continue
        date_text = str(item[0]).strip()
        try:
            day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=_CN_TZ)
            o, c, lo, hi = (float(item[1]), float(item[2]), float(item[3]), float(item[4]))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "ts_open": int(day.timestamp() * 1000),
                "open": o,
                "high": max(hi, lo, o, c),
                "low": min(hi, lo, o, c),
                "close": c,
                "volume": 0.0,
            }
        )
    rows.sort(key=lambda r: r["ts_open"])
    return rows


def _trade_date_for_intraday(now: datetime) -> datetime:
    """分时数据所属交易日：08:30 前仍算前一交易日（夜盘延续）。"""
    if now.hour * 60 + now.minute < 8 * 60 + 30:
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_intraday(payload: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """``{"times": ["20:00", ...], "data": ["883.42", ...]}`` → 升序 1 分钟行。

    分时序列从一个交易日的 20:00 夜盘开始，跨零点后进入次日，再衔接 09:00 日盘。
    """
    now = now or _cn_now()
    times = payload.get("times") if isinstance(payload, dict) else None
    prices = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(times, list) or not isinstance(prices, list):
        return []

    day = _trade_date_for_intraday(now)
    rows: list[dict[str, Any]] = []
    prev_minute: int | None = None
    current_day = day
    for t_raw, p_raw in zip(times, prices):
        text = str(t_raw).strip()
        if ":" not in text:
            continue
        try:
            hh, mm = (int(x) for x in text.split(":")[:2])
            price = float(p_raw)
        except (TypeError, ValueError):
            continue
        if prev_minute is None:
            # 首个点若在 20:00 之后，属于前一自然日的夜盘
            if hh >= 20:
                current_day = day - timedelta(days=1)
        elif hh * 60 + mm < prev_minute and prev_minute - (hh * 60 + mm) > 60:
            # 时间往回跳超过 1 小时 = 跨零点进入新交易日
            current_day = current_day + timedelta(days=1)
        prev_minute = hh * 60 + mm
        ts = current_day.replace(hour=hh, minute=mm)
        rows.append(
            {
                "ts_open": int(ts.timestamp() * 1000),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0.0,
            }
        )
    rows.sort(key=lambda r: r["ts_open"])
    return rows


def _resample_rows(rows_asc: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """把 1 分钟行按 *minutes* 对齐合并（按 ts_open 分桶，保证跨夜/跨午休不粘连）。"""
    if not rows_asc or minutes <= 1:
        return list(rows_asc)
    bucket_ms = minutes * 60 * 1000
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in rows_asc:
        key = int(row["ts_open"]) // bucket_ms * bucket_ms
        buckets.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        merged = _merge_ohlcv(buckets[key])
        merged["ts_open"] = key
        out.append(merged)
    return out


def _parse_sina_quote(text: str) -> dict[str, float] | None:
    """解析 ``var hq_str_gds_AU9999="873.00,0,873.00,873.45,878.60,862.05,..."``。

    字段：0 现价 / 4 最高 / 5 最低 / 7 昨收 / 8 今开 / 9 成交量。
    """
    if "=" not in text or '"' not in text:
        return None
    body = text.split("=", 1)[1].strip().strip(";").strip('"')
    parts = body.split(",")
    if len(parts) < 10:
        return None
    try:
        return {
            "price": float(parts[0]),
            "high": float(parts[4]),
            "low": float(parts[5]),
            "prev_close": float(parts[7]),
            "open": float(parts[8]),
            "volume": float(parts[9]),
        }
    except (TypeError, ValueError):
        return None


class SgeSource(DataSource):
    """上海黄金交易所黄金现货（Au99.99 等），日线 + 当日分时。"""

    #: 该源不提供成交量（历史日线为 0，仅当日有累计量）
    HAS_VOLUME = False

    def __init__(self) -> None:
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected: bool = False
        self._warned_no_volume: bool = False

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._connected = True
        logger.info("SgeSource connected (SGE Au99.99; 上金所现货无历史成交量)")

    def disconnect(self) -> None:
        self._connected = False
        self._symbol = ""
        self._timeframe = ""
        logger.info("SgeSource disconnected")

    # ── 元数据 ──────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return [f"{inst} {name}" for inst, name in SGE_INSTRUMENTS.items()]

    def supported_timeframes(self) -> list[str]:
        return list(_SUPPORTED_TIMEFRAMES)

    def is_symbol_available(self, symbol: str) -> bool:
        return bool(normalize_sge_symbol(symbol))

    # ── 订阅 ────────────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"不支持的周期: {timeframe!r}. 上金所现货请使用 {list(_SUPPORTED_TIMEFRAMES)} 之一"
                "（分钟周期仅有当日数据，1h 不足 20 根故不提供）"
            )
        inst = normalize_sge_symbol(symbol)
        if not inst:
            raise ValueError(
                "品种无效: 请输入 Au99.99 / Au100g / Au(T+D) / Ag(T+D) / Pt99.95"
            )
        self._symbol = inst
        self._timeframe = timeframe
        logger.info("SgeSource subscribed: %s %s", inst, timeframe)

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        logger.info("SgeSource unsubscribed")

    # ── 快照 ────────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("上金所数据源未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("未订阅黄金品种/周期")

        if not self._warned_no_volume:
            logger.warning(
                "上金所现货不发布成交量：%s 历史 K 线 volume 恒为 0，量价类判断不可用",
                self._symbol,
            )
            self._warned_no_volume = True

        try:
            if self._timeframe == "1d":
                rows_asc = self._fetch_daily(n)
            else:
                rows_asc = self._fetch_intraday(self._timeframe, n)
        except DataSourceTransientError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("上金所数据拉取失败: %s", exc)
            raise DataSourceTransientError(f"上金所数据拉取失败: {exc}") from exc

        if not rows_asc:
            raise DataSourceTransientError(
                f"未返回上金所数据: {self._symbol} {self._timeframe}"
            )

        live = _session_open()
        rows_asc = rows_asc[-max(n + 5, 30):]
        rows_newest = list(reversed(rows_asc))
        for i, row in enumerate(rows_newest):
            row["closed"] = not (i == 0 and live)

        return self._to_bars(rows_newest, n)

    # ── 抓取 ────────────────────────────────────────────────────────────────

    def _fetch_daily(self, n: int) -> list[dict[str, Any]]:
        text = _http_get(_DAILY_URL.format(inst=self._symbol))
        rows = _parse_daily(json.loads(text))
        if rows and self._symbol in _SINA_CODE:
            self._apply_realtime_to_last(rows)
        return rows

    def _fetch_intraday(self, timeframe: str, n: int) -> list[dict[str, Any]]:
        text = _http_get(_MINUTE_URL.format(inst=self._symbol))
        minute_rows = _parse_intraday(json.loads(text))
        return _resample_rows(minute_rows, _TF_MINUTES.get(timeframe, 1))

    def _apply_realtime_to_last(self, rows_asc: list[dict[str, Any]]) -> None:
        """用新浪实时快照刷新「当日」这根日线（含当日累计成交量）。"""
        quote = self._fetch_sina_quote()
        if not quote or quote["price"] <= 0:
            return
        last = rows_asc[-1]
        price = quote["price"]
        last["open"] = quote["open"] or last["open"]
        last["close"] = price
        last["high"] = max(last["high"], quote["high"], price)
        last["low"] = min(last["low"], quote["low"], price) if quote["low"] > 0 else last["low"]
        last["volume"] = quote["volume"]

    def _fetch_sina_quote(self) -> dict[str, float] | None:
        code = _SINA_CODE.get(self._symbol)
        if not code:
            return None
        try:
            text = _http_get(
                _SINA_URL.format(code=code),
                referer="https://finance.sina.com.cn/",
                timeout=10.0,
            )
            return _parse_sina_quote(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("上金所实时快照获取失败: %s", exc)
            return None

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
