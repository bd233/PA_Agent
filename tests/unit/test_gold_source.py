"""「黄金」合并数据源：品种下拉 = 黄金9999（上金所）/ 浙商积存金（银行）。"""
from __future__ import annotations

import pytest

from pa_agent.data import gold_source
from pa_agent.data.gold_source import (
    GOLD_DEFAULT_SYMBOL,
    GOLD_INSTRUMENTS,
    GoldSource,
    normalize_gold_instrument,
)


def test_instruments_are_the_two_expected_choices() -> None:
    assert GOLD_INSTRUMENTS == ("黄金9999", "浙商积存金")
    assert GOLD_DEFAULT_SYMBOL == "黄金9999"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("黄金9999", "黄金9999"),
        ("au99.99", "黄金9999"),
        ("AU9999", "黄金9999"),
        ("XAUUSD", "黄金9999"),
        ("浙商积存金", "浙商积存金"),
        ("浙商", "浙商积存金"),
        ("czb", "浙商积存金"),
        ("浙商积存金(1961543816)", "浙商积存金"),
        ("", ""),
        ("BTCUSDT", ""),
    ],
)
def test_normalize_gold_instrument(raw: str, expected: str) -> None:
    assert normalize_gold_instrument(raw) == expected


def test_list_symbols_and_timeframes() -> None:
    src = GoldSource()
    src.connect()
    assert src.list_symbols() == ["黄金9999", "浙商积存金"]
    assert src.supported_timeframes() == ["1d", "5m", "1m"]
    assert src.HAS_VOLUME is False


def test_subscribe_routes_to_sge_delegate(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeSge:
        _connected = False

        def connect(self) -> None:
            self._connected = True

        def subscribe(self, symbol: str, timeframe: str) -> None:
            calls["delegate"] = "sge"
            calls["symbol"] = symbol
            calls["tf"] = timeframe

        def latest_snapshot(self, n: int):  # noqa: ANN001
            return ["bar"]

        def unsubscribe(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    monkeypatch.setattr(gold_source.GoldSource, "_make_delegate", lambda self, kind: FakeSge())

    src = GoldSource()
    src.connect()
    src.subscribe("黄金9999", "1d")
    assert calls == {"delegate": "sge", "symbol": "Au99.99", "tf": "1d"}
    assert src.latest_snapshot(5) == ["bar"]
    assert src.current_instrument() == "黄金9999"


def test_subscribe_routes_to_bank_delegate(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeBank:
        _connected = False

        def connect(self) -> None:
            self._connected = True

        def subscribe(self, symbol: str, timeframe: str) -> None:
            calls["symbol"] = symbol
            calls["tf"] = timeframe

        def latest_snapshot(self, n: int):  # noqa: ANN001
            return []

        def unsubscribe(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    monkeypatch.setattr(gold_source.GoldSource, "_make_delegate", lambda self, kind: FakeBank())

    src = GoldSource()
    src.connect()
    src.subscribe("浙商积存金", "5m")
    assert calls == {"symbol": "浙商积存金", "tf": "5m"}


def test_subscribe_rejects_unknown_instrument_and_timeframe() -> None:
    src = GoldSource()
    src.connect()
    with pytest.raises(ValueError, match="黄金品种无效"):
        src.subscribe("BTCUSDT", "1d")
    with pytest.raises(ValueError, match="黄金不支持周期"):
        src.subscribe("黄金9999", "15m")


def test_snapshot_before_subscribe_raises() -> None:
    src = GoldSource()
    src.connect()
    with pytest.raises(RuntimeError, match="未订阅"):
        src.latest_snapshot(5)
