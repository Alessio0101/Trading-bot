"""Shared data structures exchanged between the coordinator and specialists."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class Task:
    agent_id: str
    objective: str
    pair: str = "BTC/USDT"
    timeframe: str = "1h"
    params: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: new_id("task"))
    created_at: str = field(default_factory=utcnow)


@dataclass
class Signal:
    """A trade *proposal*. It is never an order: only the risk gateway can act on it."""

    pair: str
    side: str  # "long" or "exit"
    confidence: float
    source: str
    rationale: str
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    agent_id: str
    task_id: str
    status: str  # "ok" | "error" | "needs_integration"
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=utcnow)
    finished_at: str | None = None
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signals"] = [s.to_dict() for s in self.signals]
        return d


@dataclass
class Message:
    sender: str
    recipient: str
    topic: str
    payload: dict[str, Any]
    workflow: str | None = None
    message_id: str = field(default_factory=lambda: new_id("msg"))
    sent_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
