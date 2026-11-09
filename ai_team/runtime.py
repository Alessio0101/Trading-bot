"""Execution context and runner shared by all specialists."""

from __future__ import annotations

import importlib
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .data import MarketData
from .models import Report, Signal, Task, utcnow
from .specs import BY_ID, Spec


DEFAULT_UNIVERSE = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]


class NeedsIntegration(Exception):
    """Raised by a handler whose required integration/data feed is missing."""


@dataclass
class HandlerResult:
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)


@dataclass
class AgentContext:
    task: Task
    data: MarketData
    inbox: dict[str, Report] = field(default_factory=dict)
    universe: list[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    # Read-only snapshot of the paper portfolio supplied by the gateway.
    portfolio: dict[str, Any] = field(default_factory=dict)
    gateway_health: dict[str, Any] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def ohlcv(self, pair: str | None = None, timeframe: str | None = None, limit: int = 500):
        return self.data.ohlcv(pair or self.task.pair, timeframe or self.task.timeframe, limit)

    def upstream(self, agent_id: str) -> Report | None:
        return self.inbox.get(agent_id)


def resolve_handler(spec: Spec) -> Callable[[AgentContext], HandlerResult]:
    module, func = spec.handler.split(":")
    return getattr(importlib.import_module(module), func)


def run_specialist(agent_id: str, ctx: AgentContext) -> Report:
    spec = BY_ID[agent_id]
    report = Report(agent_id=agent_id, task_id=ctx.task.task_id, status="ok", summary="")
    t0 = time.perf_counter()
    try:
        res = resolve_handler(spec)(ctx)
        report.summary, report.metrics, report.signals = res.summary, res.metrics, res.signals
        report.metrics.setdefault("data_source", ctx.data.name)
        report.metrics.setdefault("pair", ctx.task.pair)
        report.metrics.setdefault("timeframe", ctx.task.timeframe)
        report.metrics["upstream_used"] = sorted(ctx.inbox)
        for s in report.signals:
            s.source = agent_id
    except NeedsIntegration as e:
        report.status, report.summary = "needs_integration", str(e)
    except Exception as e:
        report.status = "error"
        report.summary = f"{type(e).__name__}: {e}"
        report.errors.append(traceback.format_exc(limit=3))
    report.duration_ms = round((time.perf_counter() - t0) * 1000, 1)
    report.finished_at = utcnow()
    return report


def now_ts() -> float:
    return datetime.now(UTC).timestamp()
