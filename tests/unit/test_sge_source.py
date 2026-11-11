"""SgeSource（上金所 Au99.99）解析与快照测试：全部离线，不联网。"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pa_agent.data import sge_source as sge

_CN = ZoneInfo("Asia/Shanghai")

_DAILY_PAYLOAD = {
    "time": [
        ["2026-11-09", 880.00, 878.00, 875.00, 882.00],
        ["2026-11-10", 878.00, 883.42, 877.00, 885.00],
    ]
}

_MINUTE_PAYLOAD = {
    "times": ["20:00", "23:59", "00:00", "02:29", "09:00", "11:30", "13:30", "15:30"],
    "data": ["883.42", "884.00", "884.10", "884.50", "885.00", "886.20", "886.00", "872.98"],
    "now": "16:39",
}

_SINA_TEXT = (
    'var hq_str_gds_AU9999="873.00,0,873.00,873.45,878.60,862.05,15:29:58,'
    '883.42,876.00,411012,21.00,1000.00,2026-11-11,沪金99";'
)


# ── 品种归一化 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Au99.99", "Au99.99"),
        ("AU9999", "Au99.99"),
        ("au99.99", "Au99.99"),
        ("Au99.99 黄金9999", "Au99.99"),
        ("XAUUSD", "Au99.99"),
        ("Au(T+D)", "Au(T+D)"),
        ("AUTD", "Au(T+D)"),
        ("AGTD", "Ag(T+D)"),
        ("Pt99.95", "Pt99.95"),
        ("", ""),
        ("BTCUSDT", ""),
        ("RB0 螺纹钢", ""),
    ],
)
def test_normalize_sge_symbol(raw: str, expected: str) -> None:
    assert sge.normalize_sge_symbol(raw) == expected


def test_supported_timeframes_excludes_1h() -> None:
    assert sge.SgeSource().supported_timeframes() == ["1d", "30m", "15m", "5m", "1m"]
    assert "1h" not in sge.SgeSource().supported_timeframes()


# ── 解析 ──────────────────────────────────────────────────────────────────


def test_parse_daily_reads_open_close_low_high_order() -> None:
    rows = sge._parse_daily(_DAILY_PAYLOAD)
    assert len(rows) == 2
    first = rows[0]
    assert datetime.fromtimestamp(first["ts_open"] / 1000, _CN).strftime("%Y-%m-%d") == "2026-11-09"
    assert first["open"] == 880.00
    assert first["close"] == 878.00  # 第 2 位是收盘价，不是最高价
    assert first["high"] == 882.00
    assert first["low"] == 875.00
    assert first["volume"] == 0.0
    assert rows[0]["ts_open"] < rows[1]["ts_open"]


def test_parse_daily_skips_broken_rows() -> None:
    rows = sge._parse_daily({"time": [["bad", 1, 2, 3, 4], ["2026-11-10", 1, 2, 3, 4]]})
    assert len(rows) == 1


def test_parse_intraday_rolls_over_midnight() -> None:
    now = datetime(2026, 11, 11, 10, 0, tzinfo=_CN)
    rows = sge._parse_intraday(_MINUTE_PAYLOAD, now=now)
    assert len(rows) == 8
    dates = [
        datetime.fromtimestamp(r["ts_open"] / 1000, _CN).strftime("%m-%d %H:%M") for r in rows
    ]
    assert dates[0] == "11-10 20:00"  # 夜盘归前一交易日
    assert dates[1] == "11-10 23:59"
    assert dates[2] == "11-11 00:00"  # 跨零点换日
    assert dates[-1] == "11-11 15:30"
    assert [r["ts_open"] for r in rows] == sorted(r["ts_open"] for r in rows)
    assert all(r["volume"] == 0.0 for r in rows)


def test_resample_rows_builds_ohlc() -> None:
    now = datetime(2026, 11, 11, 16, 0, tzinfo=_CN)
    minute_rows = sge._parse_intraday(_MINUTE_PAYLOAD, now=now)
    bars = sge._resample_rows(minute_rows, 30)
    assert bars, "重采样不应为空"
    for bar in bars:
        assert bar["high"] >= max(bar["open"], bar["close"], bar["low"])
        assert bar["low"] <= min(bar["open"], bar["close"], bar["high"])
        assert bar["ts_open"] % (30 * 60 * 1000) == 0
    # 13:30-15:30 三个点应在同一个 30m 桶里合并
    afternoon = [
        b for b in bars
        if datetime.fromtimestamp(b["ts_open"] / 1000, _CN).strftime("%H:%M") in {"13:30", "15:00", "15:30"}
    ]
    assert len({b["ts_open"] for b in afternoon}) <= 2


def test_parse_sina_quote_fields() -> None:
    quote = sge._parse_sina_quote(_SINA_TEXT)
    assert quote is not None
    assert quote["price"] == 873.00
    assert quote["high"] == 878.60
    assert quote["low"] == 862.05
    assert quote["prev_close"] == 883.42
    assert quote["open"] == 876.00
    assert quote["volume"] == 411012.0


def test_parse_sina_quote_rejects_empty_payload() -> None:
    assert sge._parse_sina_quote('var hq_str_gds_AU9999="";') is None


# ── 快照（monkeypatch 掉 HTTP） ────────────────────────────────────────────


def _fake_http(url: str, **_kwargs) -> str:
    if "hq.sinajs.cn" in url:
        return _SINA_TEXT
    if "Dailyhq" in url:
        return json.dumps(_DAILY_PAYLOAD)
    if "quotations" in url:
        return json.dumps(_MINUTE_PAYLOAD)
    raise AssertionError(f"unexpected url: {url}")


def test_latest_snapshot_daily_uses_realtime_for_last_bar(monkeypatch) -> None:
    monkeypatch.setattr(sge, "_http_get", _fake_http)
    src = sge.SgeSource()
    src.connect()
    src.subscribe("Au99.99", "1d")
    bars = src.latest_snapshot(5)

    assert len(bars) == 2
    assert bars[0].ts_open > bars[1].ts_open  # index 0 = 最新
    assert bars[0].close == 873.00  # 被新浪实时刷过
    assert bars[0].volume == 411012.0  # 当日累计量（历史日为 0）
    assert bars[1].close == 878.00
    assert bars[1].volume == 0.0
    assert src.HAS_VOLUME is False


def test_latest_snapshot_30m_merges_minutes(monkeypatch) -> None:
    monkeypatch.setattr(sge, "_http_get", _fake_http)
    src = sge.SgeSource()
    src.connect()
    src.subscribe("AU9999", "30m")
    bars = src.latest_snapshot(10)

    assert bars
    assert bars[0].ts_open > bars[-1].ts_open
    for bar in bars:
        assert bar.high >= bar.low
        assert bar.volume == 0.0


def test_subscribe_rejects_unsupported_timeframe() -> None:
    src = sge.SgeSource()
    src.connect()
    with pytest.raises(ValueError, match="不支持的周期"):
        src.subscribe("Au99.99", "1h")


def test_subscribe_rejects_unknown_symbol() -> None:
    src = sge.SgeSource()
    src.connect()
    with pytest.raises(ValueError, match="品种无效"):
        src.subscribe("BTCUSDT", "1d")


def test_snapshot_before_subscribe_raises() -> None:
    src = sge.SgeSource()
    src.connect()
    with pytest.raises(Exception, match="未订阅"):
        src.latest_snapshot(5)
