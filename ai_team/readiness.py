"""
"Ready for real money?" - fixed go/no-go criteria for the paper account.

The criteria are set in advance so the decision is not made on a good feeling. Passing
them is a precondition for *considering* real money, not a promise of profit, and this
module changes nothing: the system stays paper-only until a person decides otherwise and
live trading is built and reviewed separately.

Each criterion is True (met), False (not met) or None (not enough data yet).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd


MIN_DAYS = 60
MIN_TRADES = 30
MAX_DRAWDOWN = -0.10
MIN_TRADES_TO_COMPARE = 10
WIN_RATE_TOLERANCE = 0.15
MAX_FAILED_CYCLE_SHARE = 0.02


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _close_at(candles: pd.DataFrame | None, when: datetime) -> float | None:
    if candles is None or not len(candles):
        return None
    before = candles[candles.index <= pd.Timestamp(when)]
    return float(before["close"].iloc[-1]) if len(before) else None


def evaluate(
    portfolio: dict[str, Any],
    starting_equity: float | None,
    runner: dict[str, Any] | None,
    reviews: dict[str, dict[str, Any]],
    btc_candles: pd.DataFrame | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    criteria: list[dict[str, Any]] = []

    def add(key: str, name: str, ok: bool | None, value: str, target: str) -> None:
        criteria.append({"key": key, "name": name, "ok": ok, "value": value, "target": target})

    started = _ts(portfolio.get("created_at"))
    days = (now - started).total_seconds() / 86400 if started else 0.0
    add("duration", "Time on paper", days >= MIN_DAYS, f"{days:.0f} days", f">= {MIN_DAYS} days")

    realised = portfolio.get("realised") or []
    add("trades", "Closed trades", len(realised) >= MIN_TRADES, str(len(realised)), f">= {MIN_TRADES}")

    equity = portfolio.get("equity")
    ret = equity / starting_equity - 1 if equity and starting_equity else None
    add(
        "profit",
        "Net profit after fees",
        None if ret is None or len(realised) < MIN_TRADES_TO_COMPARE else ret > 0,
        "-" if ret is None else f"{ret * 100:+.2f}%",
        "> 0%",
    )

    btc_then = _close_at(btc_candles, started) if started else None
    btc_now = float(btc_candles["close"].iloc[-1]) if btc_candles is not None and len(btc_candles) else None
    btc_ret = btc_now / btc_then - 1 if btc_then and btc_now else None
    add(
        "beats_btc",
        "Better than simply holding BTC",
        None if ret is None or btc_ret is None or len(realised) < MIN_TRADES_TO_COMPARE else ret > btc_ret,
        "-" if btc_ret is None or ret is None else f"{ret * 100:+.2f}% vs BTC {btc_ret * 100:+.2f}%",
        "bot > BTC",
    )

    dd = float(portfolio.get("max_drawdown_pct") or 0.0)
    add(
        "drawdown",
        "Worst drop from a peak",
        dd >= MAX_DRAWDOWN,
        f"{dd * 100:.1f}%",
        f">= {MAX_DRAWDOWN * 100:.0f}%",
    )

    traded = {r.get("strategy") for r in realised}
    expected = [
        m["win_rate"]
        for review in reviews.values()
        for name, m in (review.get("backtest") or {}).items()
        if name in traded and m.get("win_rate") is not None
    ]
    paper_wr = sum(1 for r in realised if r["pnl"] > 0) / len(realised) if realised else None
    bt_wr = sum(expected) / len(expected) if expected else None
    ok: bool | None = None
    if paper_wr is None:
        value = "-"
    elif bt_wr is None:
        value = f"win rate {paper_wr * 100:.0f}%"
    else:
        value = f"win rate {paper_wr * 100:.0f}% vs tested {bt_wr * 100:.0f}%"
        if len(realised) >= MIN_TRADES_TO_COMPARE:
            ok = paper_wr >= bt_wr - WIN_RATE_TOLERANCE
    add(
        "matches_backtest",
        "Paper results match the backtests",
        ok,
        value,
        f"no more than {WIN_RATE_TOLERANCE * 100:.0f} points below the backtests",
    )

    done = (runner or {}).get("cycles_completed", 0)
    failed = (runner or {}).get("cycles_failed", 0)
    share = failed / (done + failed) if done + failed else None
    add(
        "reliability",
        "Runs without errors",
        None
        if share is None
        else share <= MAX_FAILED_CYCLE_SHARE and bool((runner or {}).get("alive", True)),
        "-" if share is None else f"{failed} failed of {done + failed} cycles",
        f"<= {MAX_FAILED_CYCLE_SHARE * 100:.0f}% failed",
    )

    met = sum(1 for c in criteria if c["ok"] is True)
    return {
        "ready": met == len(criteria),
        "met": met,
        "total": len(criteria),
        "criteria": criteria,
        "started_at": portfolio.get("created_at"),
        "note": (
            "Meeting every criterion is a precondition for considering real money, not a promise of profit."
        ),
    }
