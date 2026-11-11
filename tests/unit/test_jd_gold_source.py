"""银行积存金数据源（京东金融报价）单元测试：不联网。"""
from __future__ import annotations

import pytest

from pa_agent.data import factory
from pa_agent.data.jd_gold_source import (
    JD_GOLD_DEFAULT_SYMBOL,
    JD_GOLD_PRODUCTS,
    JdGoldSource,
    _parse_kline_line,
    _parse_trend_points,
    normalize_jd_gold_symbol,
)


def test_normalize_accepts_name_alias_and_sku() -> None:
    assert normalize_jd_gold_symbol("浙商积存金") == "浙商积存金"
    assert normalize_jd_gold_symbol("浙商") == "浙商积存金"
    assert normalize_jd_gold_symbol("CZB") == "浙商积存金"
    assert normalize_jd_gold_symbol("1961543816") == "浙商积存金"
    assert normalize_jd_gold_symbol("民生积存金 21001001000001") == "民生积存金"
    assert normalize_jd_gold_symbol("浙商积存金(1961543816)") == "浙商积存金"


def test_normalize_rejects_unknown() -> None:
    assert normalize_jd_gold_symbol("") == ""
    assert normalize_jd_gold_symbol("XAUUSDm") == ""
    assert normalize_jd_gold_symbol("600519") == ""


def test_parse_kline_line_uses_previous_close_as_open() -> None:
    line = [
        {"date": "20261111", "price": "870.49", "raisePercent": "-0.0386"},
        {"date": "20261010", "price": "905.46", "raisePercent": "0.0000"},
        {"date": "bad", "price": "1.0"},
    ]
    rows = _parse_kline_line(line)
    assert [r["close"] for r in rows] == [905.46, 870.49]
    assert rows[0]["ts_open"] < rows[1]["ts_open"]
    # 首根没有前收 → 开=收
    assert rows[0]["open"] == rows[0]["close"] == 905.46
    # 后继根：开 = 前一根收盘，高低包住两者
    assert rows[1]["open"] == 905.46
    assert rows[1]["high"] == 905.46
    assert rows[1]["low"] == 870.49
    assert rows[1]["close"] == 870.49


def test_parse_trend_points_are_minute_aligned() -> None:
    items = [
        {"goldPriceTime": "2026-11-11 00:01:00", "goldPrice": "868.94"},
        {"goldPriceTime": "2026-11-11 00:00:00", "goldPrice": "868.74"},
    ]
    rows = _parse_trend_points(items)
    assert [r["close"] for r in rows] == [868.74, 868.94]
    assert rows[1]["ts_open"] - rows[0]["ts_open"] == 60_000


def test_subscribe_validates_timeframe_and_symbol() -> None:
    src = JdGoldSource()
    src.connect()
    with pytest.raises(ValueError):
        src.subscribe("浙商积存金", "1h")
    with pytest.raises(ValueError):
        src.subscribe("不存在的品种", "1d")
    src.subscribe("民生", "1d")
    assert src._symbol == "民生积存金"
    assert src._sku == JD_GOLD_PRODUCTS["民生积存金"]


def test_latest_snapshot_daily_uses_close_and_zero_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    src = JdGoldSource()
    src.connect()
    src.subscribe("浙商积存金", "1d")

    line = [{"date": f"202611{d:02d}", "price": str(800 + d)} for d in range(1, 11)]
    monkeypatch.setattr(
        src,
        "_fetch_daily",
        lambda n: _parse_kline_line(line),
    )
    bars = src.latest_snapshot(5)
    assert len(bars) == 5
    assert bars[0].close == 810.0  # 最新在前
    assert all(b.volume == 0.0 for b in bars)
    assert all(b.closed for b in bars)


def test_factory_wires_jd_gold() -> None:
    assert "jd_gold" in {k for k, _ in factory.DATA_SOURCE_CHOICES}
    assert factory.default_symbol_for_kind("jd_gold") == JD_GOLD_DEFAULT_SYMBOL
    assert factory.data_source_label("jd_gold") == factory.JD_GOLD_SOURCE_LABEL
    assert isinstance(factory.create_data_source("jd_gold"), JdGoldSource)
