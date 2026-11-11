"""K 线涨跌配色开关（cn=红涨绿跌 / intl=绿涨红跌）的单元测试。"""
from __future__ import annotations

from pa_agent.gui.widgets import candle_item


def test_normalize_candle_scheme() -> None:
    assert candle_item.normalize_candle_scheme("cn") == "cn"
    assert candle_item.normalize_candle_scheme("CN") == "cn"
    assert candle_item.normalize_candle_scheme("ashare") == "cn"
    assert candle_item.normalize_candle_scheme("intl") == "intl"
    assert candle_item.normalize_candle_scheme("GLOBAL") == "intl"
    # 无法识别的值回落到 A 股习惯（本 fork 的默认）
    assert candle_item.normalize_candle_scheme("") == "cn"
    assert candle_item.normalize_candle_scheme(None) == "cn"


def test_candle_colors_follow_scheme() -> None:
    original = candle_item.get_candle_scheme()
    try:
        candle_item.set_candle_scheme("intl")
        up, down = candle_item.candle_colors()
        assert up.green() > up.red(), "国际配色：阳线应为绿色"
        assert down.red() > down.green(), "国际配色：阴线应为红色"

        candle_item.set_candle_scheme("cn")
        up, down = candle_item.candle_colors()
        assert up.red() > up.green(), "A 股配色：阳线应为红色"
        assert down.green() > down.red(), "A 股配色：阴线应为绿色"
    finally:
        candle_item.set_candle_scheme(original)


def test_set_candle_scheme_reports_change() -> None:
    original = candle_item.get_candle_scheme()
    try:
        candle_item.set_candle_scheme("cn")
        assert candle_item.set_candle_scheme("cn") is False, "同值切换不应要求重绘"
        assert candle_item.set_candle_scheme("intl") is True
    finally:
        candle_item.set_candle_scheme(original)
