"""Trade planning + matching + settlement (pure functions, no Qt, no I/O).

撮合规则（写进回测报告，避免误解）::

    as-of 第 t 根收盘后得到计划 → 从第 t+1 根开始，最多 entry_valid_bars 根内挂单有效
    限价单  做多：low <= entry 成交，成交价 = min(entry, open)
            做空：high >= entry 成交，成交价 = max(entry, open)
    突破单  做多：high >= entry 成交，成交价 = max(entry, open)
            做空：low <= entry 成交，成交价 = min(entry, open)
    市价单  下一根 open 成交
    出场    成交后的**下一根**起逐根检查；同一根内止损与止盈都命中时按止损计（保守）
    到期    回测区间结束仍未出场 → 按最后一根收盘价平仓
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from pa_agent.backtest.models import (
    DIR_LONG,
    DIR_SHORT,
    NO_ORDER_VALUES,
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    BacktestConfig,
    DecisionMeta,
    SimTrade,
    TradePlan,
)

_HALF = 1e-9

#: 阶段一的周期位置 / 趋势 / 阶段取值 → 中文（报告展示用）
CYCLE_ZH = {
    "spike": "尖峰",
    "micro_channel": "微型通道",
    "tight_channel": "窄通道",
    "normal_channel": "正常通道",
    "broad_channel": "宽通道",
    "trending_tr": "趋势型交易区间",
    "trading_range": "交易区间",
    "extreme_tr": "极端交易区间",
    "reversal": "反转",
    "unknown": "未知",
}
DIRECTION_ZH = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
    "long": "做多",
    "short": "做空",
}
PHASE_ZH = {
    "stable": "稳定",
    "transition": "转换中",
    "transitioning": "转换中",
    "climax": "高潮",
    "climaxing": "高潮",
    "spike": "尖峰中",
    "spiking": "尖峰中",
    "trending": "趋势中",
    "expanding": "扩张中",
    "contracting": "收缩中",
    "active": "进行中",
    "ending": "收尾中",
    "unknown": "未知",
}
RISK_ZH = {"none": "无", "low": "低", "medium": "中", "high": "高"}
TERMINAL_OUTCOME_ZH = {
    "active": "进行中",
    "ending": "收尾中",
    "wait": "等待边界",
    "waiting": "等待边界",
    "invalid": "已失效",
    "invalidated": "已失效",
    "completed": "已完成",
    "no_trade": "无交易",
}


def zh(value: Any, table: dict[str, str]) -> str:
    """把英文字段翻成中文，未知值原样返回。"""
    text = str(value or "").strip()
    if not text or text in ("不适用", "none", "null"):
        return "" if text in ("", "none", "null") else text
    return table.get(text, text)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def meta_from_payload(
    payload: dict[str, Any] | None, decision: dict[str, Any] | None = None
) -> DecisionMeta:
    """从「分析结果包」里抽出阶段一判断 + 终态，供报告展示。

    *payload* 可以是 ``{"decision": ..., "diagnosis": ..., "terminal": ...}``，
    也可以直接是 decision 字典（旧调用方式）。
    """
    data = payload if isinstance(payload, dict) else {}
    dec = decision if isinstance(decision, dict) else None
    if dec is None:
        inner = data.get("decision")
        dec = inner if isinstance(inner, dict) else data
    diag = data.get("diagnosis")
    diag = diag if isinstance(diag, dict) else {}
    terminal = data.get("terminal")
    terminal = terminal if isinstance(terminal, dict) else {}

    trend_ctx = diag.get("trend_context")
    trend_ctx = trend_ctx if isinstance(trend_ctx, dict) else {}
    trend = zh(trend_ctx.get("primary_direction"), DIRECTION_ZH) or zh(
        diag.get("direction"), DIRECTION_ZH
    )
    background = zh(trend_ctx.get("background_direction"), DIRECTION_ZH)

    patterns = diag.get("detected_patterns") or []
    if not isinstance(patterns, (list, tuple)):
        patterns = []
    factors = dec.get("key_factors") or []
    if not isinstance(factors, (list, tuple)):
        factors = []

    def _int(value: Any) -> int | None:
        num = _num(value)
        return None if num is None else int(round(num))

    return DecisionMeta(
        trend=trend,
        trend_background=background,
        cycle=zh(diag.get("cycle_position"), CYCLE_ZH),
        alt_cycle=zh(diag.get("alternative_cycle_position"), CYCLE_ZH),
        phase=" · ".join(
            part
            for part in (
                zh(diag.get("market_phase"), PHASE_ZH),
                zh(diag.get("spike_stage"), PHASE_ZH),
            )
            if part
        ),
        spike_stage=zh(diag.get("spike_stage"), PHASE_ZH),
        patterns=tuple(str(p) for p in patterns if p),
        conf_diag=_int(dec.get("diagnosis_confidence") or diag.get("diagnosis_confidence")),
        conf_trade=_int(dec.get("trade_confidence")),
        win_rate=_num(dec.get("estimated_win_rate")),
        terminal_id=str(terminal.get("node_id") or ""),
        terminal_outcome=str(terminal.get("outcome") or ""),
        terminal_label=str(terminal.get("label") or ""),
        reasoning=str(dec.get("reasoning") or "").strip(),
        risk=str(dec.get("risk_assessment") or "").strip(),
        invalidation=str(dec.get("invalidation_condition") or "").strip(),
        key_factors=tuple(str(f) for f in factors if f),
    )


def plan_from_decision(ts_open: int, decision: dict[str, Any] | None) -> TradePlan:
    """把 stage2 的 ``decision`` 字典转成 :class:`TradePlan`（不下单也返回对象）。"""
    dec = decision or {}
    direction = str(dec.get("order_direction") or "").strip()
    order_type = str(dec.get("order_type") or "").strip()
    cycle = None
    return TradePlan(
        ts_open=int(ts_open),
        direction=direction,
        order_type=order_type,
        entry=_num(dec.get("entry_price")),
        stop=_num(dec.get("stop_loss_price")),
        tp=_num(dec.get("take_profit_price")),
        tp2=_num(dec.get("take_profit_price_2")),
        win_rate=_num(dec.get("estimated_win_rate")),
        cycle=cycle,
        raw=dec if isinstance(dec, dict) else {},
    )


def is_no_order(plan: TradePlan) -> bool:
    return plan.order_type in NO_ORDER_VALUES or plan.direction not in (DIR_LONG, DIR_SHORT)


def _fees(
    cfg: BacktestConfig,
    *,
    entry: float,
    exit_price: float,
    qty: float,
    direction: str,
) -> float:
    buy_notional = entry * qty
    sell_notional = exit_price * qty
    commission = (buy_notional + sell_notional) * cfg.commission_rate
    # 印花税按「卖出成交额」计（A 股口径；黄金/期货按同样规则保守计）
    if direction == DIR_LONG:
        stamp = sell_notional * cfg.stamp_duty_rate
    else:
        stamp = buy_notional * cfg.stamp_duty_rate
    return commission + stamp


def _fill_entry(
    cfg: BacktestConfig, plan: TradePlan, bar: Any
) -> float | None:
    """尝试在 *bar* 上成交，返回成交价；未触发返回 None。"""
    entry = float(plan.entry or 0.0)
    if entry <= 0:
        return None
    o, h, l = float(bar.open), float(bar.high), float(bar.low)
    long_side = plan.direction == DIR_LONG

    if plan.order_type == ORDER_MARKET:
        ref = o
    elif plan.order_type == ORDER_LIMIT:
        if long_side:
            if l > entry:
                return None
            ref = min(entry, o)
        else:
            if h < entry:
                return None
            ref = max(entry, o)
    elif plan.order_type == ORDER_STOP:
        if long_side:
            if h < entry:
                return None
            ref = max(entry, o)
        else:
            if l > entry:
                return None
            ref = min(entry, o)
    else:  # 未识别类型按市价处理
        ref = o

    slip = ref * cfg.slippage_rate
    return ref + slip if long_side else ref - slip


def _exit_price(cfg: BacktestConfig, plan: TradePlan, bar: Any) -> tuple[float, str] | None:
    """检查一根 K 线是否触发止损/止盈。"""
    o, h, l = float(bar.open), float(bar.high), float(bar.low)
    stop = float(plan.stop or 0.0)
    tp = plan.tp
    long_side = plan.direction == DIR_LONG

    if stop > 0:
        hit_stop = (l <= stop) if long_side else (h >= stop)
    else:
        hit_stop = False
    hit_tp = False
    if tp:
        hit_tp = (h >= tp) if long_side else (l <= tp)

    # 同一根里都命中：按止损（保守）
    if hit_stop:
        # 跳空穿越止损价时按开盘价成交（更差）
        if long_side and o < stop:
            price = o
        elif not long_side and o > stop:
            price = o
        else:
            price = stop
        slip = price * cfg.slippage_rate
        return (price - slip if long_side else price + slip), "止损"
    if hit_tp and tp:
        price = tp
        slip = price * cfg.slippage_rate
        return (price - slip if long_side else price + slip), "止盈"
    return None


def simulate_trade(
    plan: TradePlan,
    bars_asc: Sequence[Any],
    asof_index: int,
    cfg: BacktestConfig,
    *,
    capital: float,
) -> SimTrade | None:
    """从 ``asof_index`` 之后开始撮合；未成交返回 None。

    *bars_asc* 为时间升序（旧→新），``asof_index`` 是产生该计划的那根 K 线。
    """
    if not plan.is_actionable:
        return None
    last = len(bars_asc) - 1
    if asof_index >= last:
        return None

    fill_idx = -1
    fill_price = 0.0
    deadline = min(last, asof_index + cfg.entry_valid_bars)
    for i in range(asof_index + 1, deadline + 1):
        price = _fill_entry(cfg, plan, bars_asc[i])
        if price is not None and price > 0:
            fill_idx = i
            fill_price = price
            break
    if fill_idx < 0:
        return None

    # 仓位：按当前权益 × 仓位比例，按手数向下取整
    budget = max(capital, 0.0) * cfg.position_pct
    lot = max(int(cfg.lot_size), 1)
    qty = math.floor(budget / max(fill_price, _HALF) / lot) * lot
    if qty <= 0:
        return None

    exit_idx = -1
    exit_price = 0.0
    reason = ""
    for i in range(fill_idx + 1, last + 1):
        hit = _exit_price(cfg, plan, bars_asc[i])
        if hit is not None:
            exit_price, reason = hit
            exit_idx = i
            break
    if exit_idx < 0:
        exit_idx = last
        exit_price = float(bars_asc[last].close)
        reason = "到期平仓"

    hit_tp2 = False
    if plan.tp2:
        for i in range(fill_idx + 1, exit_idx + 1):
            bar = bars_asc[i]
            if plan.direction == DIR_LONG and float(bar.high) >= float(plan.tp2):
                hit_tp2 = True
                break
            if plan.direction == DIR_SHORT and float(bar.low) <= float(plan.tp2):
                hit_tp2 = True
                break

    sign = 1.0 if plan.direction == DIR_LONG else -1.0
    gross = (exit_price - fill_price) * qty * sign
    fees = _fees(cfg, entry=fill_price, exit_price=exit_price, qty=qty, direction=plan.direction)
    pnl = gross - fees
    notional = fill_price * qty
    pnl_pct = pnl / notional if notional else 0.0

    return SimTrade(
        plan_ts=int(plan.ts_open),
        entry_ts=int(bars_asc[fill_idx].ts_open),
        exit_ts=int(bars_asc[exit_idx].ts_open),
        direction=plan.direction,
        order_type=plan.order_type,
        entry=fill_price,
        exit=exit_price,
        stop=float(plan.stop or 0.0),
        tp=plan.tp,
        tp2=plan.tp2,
        qty=float(qty),
        pnl=pnl,
        pnl_pct=pnl_pct,
        exit_reason=reason,
        equity_after=capital + pnl,
        hit_tp2=hit_tp2,
        bars_held=exit_idx - fill_idx,
    )


def equity_points(
    trades: Iterable[SimTrade], initial_capital: float
) -> list[tuple[int, float]]:
    """按平仓时间生成权益曲线（含起点）。"""
    points: list[tuple[int, float]] = []
    if trades:
        first_ts = min(int(t.plan_ts) for t in trades)
        points.append((first_ts, initial_capital))
    for t in sorted(trades, key=lambda x: x.exit_ts):
        points.append((int(t.exit_ts), float(t.equity_after)))
    return points
