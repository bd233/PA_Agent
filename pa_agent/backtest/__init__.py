"""Walk-forward backtest: replay AI decisions bar by bar and settle PnL."""
from __future__ import annotations

from pa_agent.backtest.engine import plan_from_decision, simulate_trade
from pa_agent.backtest.models import (
    BacktestConfig,
    BacktestResult,
    SimTrade,
    StepLog,
    TradePlan,
)
from pa_agent.backtest.runner import BacktestRunner
from pa_agent.backtest.report import render_html, trades_to_csv

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "BacktestRunner",
    "SimTrade",
    "StepLog",
    "TradePlan",
    "plan_from_decision",
    "render_html",
    "simulate_trade",
    "trades_to_csv",
]
